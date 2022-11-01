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

import argparse
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from time import sleep
from typing import Callable, ContextManager, Union
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen
from xml.etree import ElementTree as et
from zipfile import ZipFile

DESKTOP_FILE_NAME = "olive2022.desktop"
DESKTOP_FILE_ROOT = Path.home() / ".local" / "share" / "applications"
NAMESPACE_OLIVEARCHIVE = uuid.UUID("835a9728-a1f7-4d0f-82f8-cd0da8838673")
SINFONIA_TIER1_URL = "https://cmu.findcloudlet.org"

try:
    from contextlib import nullcontext
except ImportError:
    from contextlib import contextmanager

    @contextmanager  # type: ignore
    def nullcontext(enter_result=None):
        yield enter_result


def vmnetx_url_to_uuid(url: str) -> uuid.UUID:
    """Canonicalize VMNetX URL and derive Sinfonia backend UUID."""
    _schema, netloc, path, _params, _query, _fragment = urlparse(url)
    canonical_url = urlunparse(("vmnetx+https", netloc, path, None, None, None))
    return uuid.uuid5(NAMESPACE_OLIVEARCHIVE, canonical_url)


def launch(args: argparse.Namespace) -> None:
    """Launch a Virtual Machine (VM) image with Sinfonia."""
    sinfonia_uuid = vmnetx_url_to_uuid(args.url)

    subprocess.run(
        args.dry_run
        + [
            args.tier3,
            SINFONIA_TIER1_URL,
            str(sinfonia_uuid),
            sys.argv[0],
            "stage2",
        ],
        check=True,
    )


def stage2(args: argparse.Namespace) -> None:
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
        return

    # Other viewers accept host:display on the command line
    for app in ["gvncviewer", "vncviewer"]:
        viewer = which(app)
        if viewer is not None:
            subprocess.run(args.dry_run + [viewer, "vmi-vnc:0"], check=True)
            return

    print("Failed to find a local vnc-viewer candidate")
    sleep(10)


def convert(args: argparse.Namespace) -> None:
    """Retrieve VMNetX image and convert to containerDisk + Sinfonia recipe."""
    RECIPES = Path("RECIPES")

    assert not args.dry_run and "Dry run not implemented for convert"

    tmp_ctx: Union[ContextManager[str], TemporaryDirectory] = (
        nullcontext(args.tmp_dir)
        if args.tmp_dir is not None
        else TemporaryDirectory(dir="/var/tmp")
    )
    with tmp_ctx as temporary_directory:
        tmpdir = Path(temporary_directory)
        VMNETX_PACKAGE = tmpdir / "vmnetx-package.zip"
        DISK_IMG = tmpdir / "disk.img"
        DISK_QCOW = tmpdir / "disk.qcow2"
        DOCKERFILE = tmpdir / "Dockerfile"
        DOCKERIGNORE = tmpdir / ".dockerignore"

        tmpdir.mkdir(exist_ok=True)

        sinfonia_uuid = vmnetx_url_to_uuid(args.url)
        print("UUID:", sinfonia_uuid)

        # fetch vmnetx package
        if args.vmnetx_package is None:
            _schema, netloc, path, params, query, fragment = urlparse(args.url)
            url = urlunparse(("https", netloc, path, params, query, fragment))
            print("Fetching", url)
            with urlopen(url) as src, VMNETX_PACKAGE.open("wb") as dst:
                total = int(src.headers["content-length"])
                copied = 0
                while True:
                    buf = src.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    dst.write(buf)
                    copied += len(buf)
                    pct = 100 * copied // total
                    print(f"\r\t{pct}%", end="", flush=True)
                print()
        else:
            VMNETX_PACKAGE = args.vmnetx_package

        # extract metadata and disk image
        with ZipFile(VMNETX_PACKAGE) as z:
            package_description = et.XML(z.read("vmnetx-package.xml"))
            vmi_fullname = package_description.attrib["name"]
            while vmi_fullname in ["", "Virtual Machine"]:
                vmi_fullname = input("VM image name: ")
            print(vmi_fullname)

            domain = et.XML(z.read("domain.xml"))
            cpus = int(domain.findtext("vcpu", default="1"))
            memory = int(domain.findtext("memory", default="65536")) // 1024
            print("cpus", cpus, "memory", memory)

            # extract disk image
            print("Extracting disk image")
            z.extract("disk.img", path=tmpdir)

        if args.tmp_dir is None and args.vmnetx_package is None:
            VMNETX_PACKAGE.unlink()

        # convert disk image
        print("Recompressing disk image")
        subprocess.run(
            [
                "qemu-img",
                "convert",
                "-c",
                "-p",
                "-O",
                "qcow2",
                str(DISK_IMG.resolve()),
                str(DISK_QCOW.resolve()),
            ],
            check=True,
        )
        compression = 100 - 100 * DISK_QCOW.stat().st_size // DISK_IMG.stat().st_size
        if compression != 0:
            print(f"compression savings {compression}%")

        if args.tmp_dir is None:
            DISK_IMG.unlink()

        # create container
        print("Creating containerDisk image")
        DOCKER_TAG = f"{args.registry}/{sinfonia_uuid}:latest"
        DOCKERIGNORE.write_text(
            """\
*
!Dockerfile
!*.qcow2
"""
        )
        DOCKERFILE.write_text(
            f"""\
FROM scratch
LABEL org.opencontainers.image.url="https://olivearchive.org" \
      org.opencontainers.image.title="{vmi_fullname}"
ADD --chown=107:107 disk.qcow2 /disk/
"""
        )
        subprocess.run(
            ["docker", "build", "-t", DOCKER_TAG, str(DOCKERFILE.parent.resolve())],
            check=True,
        )

        if args.tmp_dir is None:
            DOCKERIGNORE.unlink()
            DOCKERFILE.unlink()
            DISK_QCOW.unlink()
            tmpdir.rmdir()

            if args.deploy_token is None and not input(
                "Ok to push non-restricted image? [yes/no] "
            ).lower().startswith("yes"):
                sys.exit()

            # upload container
            print("Publishing containerDisk image")
            subprocess.run(["docker", "push", DOCKER_TAG], check=True)
            subprocess.run(
                ["docker", "image", "rm", DOCKER_TAG],
                check=True,
                stdout=subprocess.DEVNULL,
            )

    # create Sinfonia recipe
    print("Creating Sinfonia recipe", sinfonia_uuid)

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

    RECIPE = (RECIPES / str(sinfonia_uuid)).with_suffix(".yaml")
    RECIPE.parent.mkdir(exist_ok=True)
    RECIPE.write_text(
        f"""\
description: "{vmi_fullname}"
chart: https://cmusatyalab.github.io/olive2022/vmi
version: 0.1.2
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
    input("Done, hit return to quit\n")


def install(args: argparse.Namespace) -> None:
    """Create and install desktop file to handle VMNetX URLs."""
    # Make sure sinfonia is installed, not reliable as it fails to account for
    # things like running from a local venv.
    tier3 = args.tier3 or which("sinfonia-tier3")
    if tier3 is None:
        print("\n!!! Couldn't find 'sinfonia-tier3', make sure it is installed !!!\n")

    uninstall(args)

    try:
        app = Path.cwd().joinpath(sys.argv[0]).resolve(strict=True)
    except FileNotFoundError:
        # workaround for poetry bug #995
        app = Path(sys.executable).parent.joinpath(sys.argv[0]).resolve(strict=True)

    desktop_file_content = f"""\
[Desktop Entry]
Type=Application
Version=1.0
Name=Olive Archive {args.handler.capitalize()}
NoDisplay=true
Comment=Execute Olive Archive virtual machines with Sinfonia
Path=/tmp
Exec=x-terminal-emulator -e "{app} {args.handler} --tier3={tier3} '%u'"
MimeType=x-scheme-handler/vmnetx;x-scheme-handler/vmnetx+http;x-scheme-handler/vmnetx+https;
"""
    with TemporaryDirectory() as tmpdir:
        if args.dry_run:
            tmpfile = Path(DESKTOP_FILE_NAME)
            print(f"cat {tmpfile} << EOF\n{desktop_file_content}EOF")
        else:
            tmpfile = Path(tmpdir) / DESKTOP_FILE_NAME
            tmpfile.write_text(desktop_file_content, encoding="utf8")

        subprocess.run(
            args.dry_run
            + [
                "desktop-file-install",
                f"--dir={DESKTOP_FILE_ROOT}",
                "--delete-original",
                "--rebuild-mime-info-cache",
                str(tmpfile),
            ],
            check=True,
        )


def uninstall(args: argparse.Namespace) -> None:
    """Remove desktop file that defines the VMNetX URL handler."""
    desktop_file = DESKTOP_FILE_ROOT / DESKTOP_FILE_NAME

    if args.dry_run:
        print("rm", desktop_file)
    elif desktop_file.exists():
        desktop_file.unlink()


def add_subcommand(
    subp: argparse._SubParsersAction, func: Callable[[argparse.Namespace], None]
) -> argparse.ArgumentParser:
    """Helper to add a subcommand to argparse."""
    subparser = subp.add_parser(
        func.__name__, help=func.__doc__, description=func.__doc__
    )
    subparser.set_defaults(func=func)
    return subparser


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-n", "--dry-run", action="store_const", const=["echo"], default=[]
    )
    parser.set_defaults(func=lambda _: parser.print_help())

    subparsers = parser.add_subparsers(title="subcommands")

    # launch
    launch_parser = add_subcommand(subparsers, launch)
    launch_parser.add_argument(
        "--tier3", default="sinfonia-tier3", help="path to sinfonia-tier3 executable"
    )
    launch_parser.add_argument("url", metavar="VMNETX_URL")

    # install
    install_parser = add_subcommand(subparsers, install)
    install_parser.add_argument("--tier3", help="path to sinfonia-tier3 executable")
    install_parser.add_argument("handler", choices=["launch", "convert"])

    # uninstall
    add_subcommand(subparsers, uninstall)

    # convert
    convert_parser = add_subcommand(subparsers, convert)
    convert_parser.add_argument(
        "--tmp-dir",
        help="directory to keep intermediate files",
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
    parsed_args.func(parsed_args)


if __name__ == "__main__":
    main()