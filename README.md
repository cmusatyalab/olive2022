# Olive 2022

Edge-native virtual desktop application that uses the
[Sinfonia](https://github.com/cmusatyalab/sinfonia) framework to discover a
nearby cloudlet to run the virtual machine.

Virtual machine images from [Olivearchive](https://olivearchive.org) are
converted from their original vmnetx package format to a containerDisk
that can be executed with KubeVirt. The containerDisk images can be pushed into
a private Docker registry.


## Installation

Olive2022 depends on an available VNC client. I've found that virt-viewer from
libvirt generally does a good job. On a Debian/Ubuntu system this can be
installed with.

```
sudo apt install virt-viewer
```

It is best to manage the installation of olive2022 in a separate virtualenv with
[pipx](https://pypa.github.io/pipx/installation/).

```
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Once these dependencies are in place, installation should be fairly
straightforward, even specifying a python version should only be necessary if
the system default happens to be older than Python-3.7.

```
pipx install olive2022
```

If installation fails at any point, there are various troubleshooting tips at
the end of this document.


## Usage

`olive2022 install` creates a .desktop file to declare a handler for
vmnetx+https URLs.

When you then 'Launch' a virtual machine from the Olivearchive website, the
handler will execute `olive2022 launch` with the VMNetX URL for the virtual
machine image.


## Internals

`olive2022 launch` hashes the VMNetX URL to a Sinfonia UUID, and uses
`sinfonia-tier3` to request the relevant backend to be started on a nearby
cloudlet. When deployment has started, `sinfonia-tier3` will create a local
wireguard tunnel endpoint and runs `olive2022 stage2` which waits for the
deployment to complete by probing if the VNC endpoint has become accessible.
It will then try to run remote-viewer (from the virt-viewer package),
gvncviewer, or vncviewer.


## Converting VMNetX packages

`olive2022 convert` will take a VMNetX URL, download the vmnetx format package
file and convert it to a containerDisk image and associated Sinfonia recipe.
The Docker registry to push the containerDisk image to can be set with the
`OLIVE2022_REGISTRY` environment variable. If it is a private repository, the
necessary pull credentials to add to the recipe can be specified with
`OLIVE2022_CREDENTIALS=<username>:<access_token>`.


## Installation troubleshooting

#### `/usr/bin/python3: No module named pip`

Pip is not installed on your system. On Debian/Ubuntu systems, to reduce the
chance of interfering with packaged Python modules, the default Python
installation does not install pip and even disables the `python3 -m ensurepip`
way of installing a recent version of the pip package manager. You have to
install the python3-pip and python3-venv packages.

```
sudo apt install python3-pip python3-venv
```

#### `pipx: command not found`

`python3 -m pipx ensurepath` is only able to fix the PATH environment for
some (mostly bourne-like) shells. If you are using bash/sh/fish/zsh it may be
sufficient to restart your terminal to pick up the new path.

With csh/tcsh you will probably have to add the following to your `.login` or
`.cshrc` files and/or run `rehash` to pick up any new binaries.

```
set path = ( $path $HOME/.local/bin )
```

#### `ERROR: Could not find a version that satisfies the requirement olive2022 (from versions: none)`

Because Olive2022 depends on Python-3.7 or newer, installation fails with this
error when the default Python interpreter is older. On Ubuntu 18.04 you can
install a newer Python interpreter and explicitly specify it as an alternate
interpreter version when installing with pipx.

```
sudo apt install python3.8
pipx install --python python3.8 olive2022
```
