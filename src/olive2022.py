#!/usr/bin/env python3
#
# Copyright (c) 2022 Carnegie Mellon University
#
# SPDX-License-Identifier: MIT
#
"""
Launch an Olive Archive virtual machine on a Sinfonia-Tier2 cloudlet and
connect over the Sinfonia-created wireguard tunnel with a vnc client.
"""

__version__ = "0.1.3"

import argparse
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from shutil import copyfileobj, which
from tempfile import TemporaryDirectory
from time import sleep
from typing import Callable, Tuple
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen
from xml.etree import ElementTree as et
from zipfile import ZipFile

from sinfonia_tier3 import sinfonia_tier3
from tqdm import tqdm
from xdg import xdg_data_dirs, xdg_data_home

DESKTOP_FILE_NAME = "olive2022.desktop"
NAMESPACE_OLIVEARCHIVE = uuid.UUID("835a9728-a1f7-4d0f-82f8-cd0da8838673")
SINFONIA_TIER1_URL = "https://cmu.findcloudlet.org"


def vmnetx_url_to_uuid(url: str) -> uuid.UUID:
    """Canonicalize VMNetX URL and derive Sinfonia backend UUID."""
    _schema, netloc, path, _params, _query, _fragment = urlparse(url)
    canonical_url = urlunparse(("vmnetx+https", netloc, path, None, None, None))
    return uuid.uuid5(NAMESPACE_OLIVEARCHIVE, canonical_url)


def launch(args: argparse.Namespace) -> int:
    """Launch a Virtual Machine (VM) image with Sinfonia."""
    sinfonia_uuid = vmnetx_url_to_uuid(args.url)

    if args.dry_run:
        print(
            "sinfonia_tier3",
            SINFONIA_TIER1_URL,
            sinfonia_uuid,
            sys.executable,
            "-m",
            "olive2022",
            "stage2",
        )
        return 0

    return sinfonia_tier3(
        SINFONIA_TIER1_URL, sinfonia_uuid, [sys.executable, "-m", "olive2022", "stage2"]
    )


def stage2(args: argparse.Namespace) -> int:
    """Wait for deployment and start a VNC client (used by launch/sinfonia)."""
    print("Waiting for VNC server to become available", end="", flush=True)
    while True:
        print(".", end="", flush=True)
        try:
            with socket.create_connection(("vmi-vnc", 5900), 1.0) as sockfd:
                sockfd.settimeout(1.0)
                handshake = sockfd.recv(3)
                if handshake.startswith(b"RFB"):
                    break
        except (socket.gaierror, ConnectionRefusedError, socket.timeout):
            pass
        sleep(1)
    print()

    # virt-viewer expects an URL
    viewer = which("remote-viewer")
    if viewer is not None:
        subprocess.run(args.dry_run + [viewer, "vnc://vmi-vnc:5900"], check=True)
        return 0

    # Other viewers accept host:display on the command line
    for app in ["gvncviewer", "vncviewer"]:
        viewer = which(app)
        if viewer is not None:
            subprocess.run(args.dry_run + [viewer, "vmi-vnc:0"], check=True)
            return 0

    print("Failed to find a local vnc-viewer candidate")
    sleep(10)
    return 1


def _fetch_vmnetx(vmnetx_url: str, tmpdir: Path) -> Path:
    """Fetch a vmnetx package from the given URL."""
    _, netloc, path, params, query, fragment = urlparse(vmnetx_url)
    url = urlunparse(("https", netloc, path, params, query, fragment))

    vmnetx_package = tmpdir / "vmnetx-package.zip"

    print("Fetching", url)
    with urlopen(url) as response:
        total = int(response.headers["content-length"])
        with tqdm.wrapattr(response, "read", total=total) as src:
            with vmnetx_package.open("wb") as dst:
                copyfileobj(src, dst)
    return vmnetx_package


def _parse_vmnetx_package_xml(vmnetx_package_xml: bytes) -> str:
    """Extract the virtual machine name from vmnetx-package.xml.
    This is normally added by Olivearchive based on the archive meta-data.
    An 'unarchived' package may be missing the virtual machine name.
    """
    package_description = et.XML(vmnetx_package_xml)
    vmi_fullname = package_description.attrib["name"]

    while vmi_fullname in ["", "Virtual Machine"]:
        vmi_fullname = input("VM image name: ")

    return vmi_fullname


def _parse_domain_xml(domain_xml: bytes) -> Tuple[int, int]:
    """Extract cpu and memory requirements from domain.xml."""
    domain = et.XML(domain_xml)
    cpus = int(domain.findtext("vcpu", default="1"))
    memory = int(domain.findtext("memory", default="65536")) // 1024
    return cpus, memory


def _recompress_disk(disk_img: Path, tmpdir: Path) -> Path:
    """Recompress disk.img to disk.qcow."""
    disk_qcow = tmpdir / "disk.qcow2"
    subprocess.run(
        [
            "qemu-img",
            "convert",
            "-c",
            "-p",
            "-O",
            "qcow2",
            str(disk_img.resolve()),
            str(disk_qcow.resolve()),
        ],
        check=True,
    )
    compression = 100 - 100 * disk_qcow.stat().st_size // disk_img.stat().st_size
    if compression != 0:
        print(f"compression savings {compression}%")
    return disk_qcow


def _create_containerdisk(
    args: argparse.Namespace, tmpdir: Path, vmi_fullname: str, sinfonia_uuid: uuid.UUID
) -> str:
    docker_tag = f"{args.registry}/{sinfonia_uuid}:latest"
    dockerfile = tmpdir / "Dockerfile"
    dockerignore = tmpdir / ".dockerignore"

    dockerignore.write_text(
        """\
*
!Dockerfile
!*.qcow2
"""
    )
    dockerfile.write_text(
        f"""\
FROM scratch
LABEL org.opencontainers.image.url="https://olivearchive.org" \
      org.opencontainers.image.title="{vmi_fullname}"
ADD --chown=107:107 disk.qcow2 /disk/
"""
    )
    subprocess.run(
        ["docker", "build", "-t", docker_tag, str(tmpdir.resolve())], check=True
    )

    if args.tmp_dir is None:
        dockerignore.unlink()
        dockerfile.unlink()

    return docker_tag


def _publish_containerdisk(args: argparse.Namespace, docker_tag: str) -> None:
    if args.deploy_token is None and not input(
        "Ok to push non-restricted image? [yes/no] "
    ).lower().startswith("yes"):
        sys.exit()

    # upload container
    print("Publishing containerDisk image")
    subprocess.run(["docker", "push", docker_tag], check=True)
    subprocess.run(
        ["docker", "image", "rm", docker_tag], check=True, stdout=subprocess.DEVNULL
    )


def _create_recipe(
    args: argparse.Namespace,
    vmi_fullname: str,
    sinfonia_uuid: uuid.UUID,
    cpus: int,
    memory: int,
) -> None:
    recipes = Path("RECIPES")

    if args.deploy_token is not None:
        registry, _ = args.registry.split("/", 1)
        username, password = args.deploy_token.split(":", 1)
        credentials = f"""\
  containerDiskCredentials:
    registry: "{registry}"
    username: "{username}"
    password: "{password}"
  restricted: true
"""
    else:
        credentials = "  restricted: false"

    recipe = (recipes / str(sinfonia_uuid)).with_suffix(".yaml")
    recipe.parent.mkdir(exist_ok=True)
    recipe.write_text(
        f"""\
description: "{vmi_fullname}"
chart: https://cmusatyalab.github.io/olive2022/vmi
version: 0.1.4
values:
  containerDisk:
    repository: "{args.registry}"
    name: "{sinfonia_uuid}"
    bus: sata
  resources:
    requests:
      cpu: {cpus}
      memory: {memory}Mi
  virtvnc:
    fullnameOverride: vmi
"""
        + credentials
    )


def convert(args: argparse.Namespace) -> int:
    """Retrieve VMNetX image and convert to containerDisk + Sinfonia recipe."""
    if args.dry_run:
        print("Dry run not implemented for convert")
        return 1

    with TemporaryDirectory(dir="/var/tmp") as temporary_directory:
        if args.tmp_dir:
            temporary_directory = args.tmp_dir
        tmpdir = Path(temporary_directory)
        tmpdir.mkdir(exist_ok=True)

        sinfonia_uuid = vmnetx_url_to_uuid(args.url)
        print("UUID:", sinfonia_uuid)

        # fetch vmnetx package
        vmnetx_package = (
            _fetch_vmnetx(args.url, tmpdir)
            if args.vmnetx_package is None
            else Path(args.vmnetx_package)
        )

        # extract metadata and disk image
        with ZipFile(vmnetx_package) as zipfile:
            vmnetx_package_xml = zipfile.read("vmnetx-package.xml")
            vmi_fullname = _parse_vmnetx_package_xml(vmnetx_package_xml)
            print(vmi_fullname)

            domain_xml = zipfile.read("domain.xml")
            cpus, memory = _parse_domain_xml(domain_xml)
            print("cpus", cpus, "memory", memory)

            # extract disk image
            print("Extracting disk image")
            zipfile.extract("disk.img", path=tmpdir)
            disk_img = tmpdir / "disk.img"

        if args.tmp_dir is None and args.vmnetx_package is None:
            vmnetx_package.unlink()

        # convert disk image
        print("Recompressing disk image")
        disk_qcow = _recompress_disk(disk_img, tmpdir)

        if args.tmp_dir is None:
            disk_img.unlink()

        # create containerdisk image
        print("Creating containerDisk image")
        docker_tag = _create_containerdisk(
            args, disk_qcow.parent, vmi_fullname, sinfonia_uuid
        )

        if args.tmp_dir is None:
            disk_qcow.unlink()
            tmpdir.rmdir()

            _publish_containerdisk(args, docker_tag)

    # create Sinfonia recipe
    print("Creating Sinfonia recipe", sinfonia_uuid)
    _create_recipe(args, vmi_fullname, sinfonia_uuid, cpus, memory)

    input("Done, hit return to quit\n")
    return 0


def install(args: argparse.Namespace) -> int:
    """Create and install desktop file to handle VMNetX URLs."""
    uninstall(args)

    handler = "convert" if args.convert else "launch"

    desktop_file_content = f"""\
[Desktop Entry]
Type=Application
Version=1.0
Name=Olive Archive {handler.capitalize()}
NoDisplay=true
Comment=Execute Olive Archive virtual machines with Sinfonia
Path=/tmp
Exec=x-terminal-emulator -e "{sys.executable} -m olive2022 {handler} '%u'"
MimeType=x-scheme-handler/vmnetx;x-scheme-handler/vmnetx+http;x-scheme-handler/vmnetx+https;
"""
    with TemporaryDirectory() as tmpdir:
        if args.dry_run:
            tmpfile = Path(DESKTOP_FILE_NAME)
            print(f"cat {tmpfile} << EOF\n{desktop_file_content}EOF")
        else:
            tmpfile = Path(tmpdir) / DESKTOP_FILE_NAME
            tmpfile.write_text(desktop_file_content, encoding="utf8")

        if args.user:
            desktop_file_root = xdg_data_home() / "applications"
            extra_args = [f"--dir={desktop_file_root}"]
        else:
            extra_args = []

        print("Installing olive2022.desktop")
        try:
            subprocess.run(
                args.dry_run
                + ["desktop-file-install"]
                + extra_args
                + ["--delete-original", "--rebuild-mime-info-cache", str(tmpfile)],
                check=True,
            )
        except subprocess.CalledProcessError:
            print(
                "Failed to install olive2022.desktop file",
                "(you may need to use sudo)" if not args.user else "",
            )
    return 0


def uninstall(args: argparse.Namespace) -> int:
    """Remove desktop file that defines the VMNetX URL handler."""
    data_dirs = xdg_data_dirs()
    data_dirs.insert(0, xdg_data_home())

    for data_dir in data_dirs:
        desktop_file = data_dir / "applications" / DESKTOP_FILE_NAME

        if args.dry_run:
            print("rm -f", desktop_file)
        elif desktop_file.exists():
            print("Removing", desktop_file)
            desktop_file.unlink()
    return 0


def add_subcommand(
    subp: "argparse._SubParsersAction[argparse.ArgumentParser]",
    func: Callable[[argparse.Namespace], int],
) -> argparse.ArgumentParser:
    """Helper to add a subcommand to argparse."""
    subparser = subp.add_parser(
        func.__name__, help=func.__doc__, description=func.__doc__
    )
    subparser.set_defaults(func=func)
    return subparser


def main() -> int:
    """main entrypoint"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_const", const=["echo"], default=[]
    )
    parser.set_defaults(func=lambda _: parser.print_help())

    subparsers = parser.add_subparsers(title="subcommands")

    # launch
    launch_parser = add_subcommand(subparsers, launch)
    launch_parser.add_argument("url", metavar="VMNETX_URL")

    # install
    install_parser = add_subcommand(subparsers, install)
    install_parser.add_argument(
        "--user", action="store_true", help="install in user specific location"
    )
    install_parser.add_argument(
        "--no-user",
        "--system",
        dest="user",
        action="store_false",
        help="install in system path",
    )
    install_parser.set_defaults(user=True)
    install_parser.add_argument(
        "--convert",
        action="store_true",
        help="run 'olive2022 convert' instead of 'olive2022 launch'",
    )

    # uninstall
    add_subcommand(subparsers, uninstall)

    # convert
    convert_parser = add_subcommand(subparsers, convert)
    convert_parser.add_argument(
        "--tmp-dir", help="directory to keep intermediate files"
    )
    convert_parser.add_argument(
        "--registry",
        default=os.environ.get(
            "OLIVE2022_REGISTRY",
            "registry.cmusatyalab.org/cloudlet-discovery/olive2022",
        ),
        help="registry where to store containerDisk [OLIVE2022_REGISTRY]",
    )
    convert_parser.add_argument(
        "--deploy-token",
        default=os.environ.get("OLIVE2022_CREDENTIALS"),
        help="docker pull credentials to add to recipe [OLIVE2022_CREDENTIALS]",
    )
    convert_parser.add_argument("url", metavar="VMNETX_URL")
    convert_parser.add_argument("vmnetx_package", nargs="?")

    # stage2
    add_subcommand(subparsers, stage2)

    parsed_args = parser.parse_args()
    func: Callable[[argparse.Namespace], int] = parsed_args.func
    return func(parsed_args)


if __name__ == "__main__":
    sys.exit(main())
