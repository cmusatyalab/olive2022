# Olive 2022

Edge-native virtual desktop application that uses the
[Sinfonia](https://github.com/cmusatyalab/sinfonia) framework to discover a
nearby cloudlet to run the virtual machine.

Virtual machine images from [Olivearchive](https://olivearchive.org) are
converted from their original vmnetx package format to a containerDisk
that can be executed with KubeVirt. The containerDisk images can be pushed into
a private Docker registry.


## Usage

`olive2022 install` creates a .desktop file to declare a handler for vmnetx+https URLs.

When you then 'Launch' a virtual machine from the Olivearchive website, the
handler will execute `olive2022 launch` with the VMNetX URL for the virtual machine image.


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
