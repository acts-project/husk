# GPU Passthrough PoC Findings

## Environment

Host: Fedora 42 on Lenovo laptop with Intel iGPU + NVIDIA RTX 500 Ada dGPU.

Confirmed working:

```text
Intel VT-x enabled ✅
Intel IOMMU / DMAR active ✅
NVIDIA GPU isolated in its own IOMMU group ✅
GPU bound to vfio-pci on host ✅
libvirt/QEMU VM starts with hostdev GPU ✅
Guest OS sees passed-through NVIDIA GPU ✅
```

## Device Details

```text
Host PCI: 01:00.0
Device: NVIDIA AD107GLM [RTX 500 Ada Generation Laptop GPU]
PCI ID: 10de:28ba
IOMMU group: 17
```

Host binding:

```text
Kernel driver in use: vfio-pci
```

## VM Validation

Guest PCI visibility:

```text
vendor=0x10de
device=0x28ba
```

Fedora guest:

```text
07:00.0 3D controller [0302]: NVIDIA Corporation AD107GLM [RTX 500 Ada Generation Laptop GPU] [10de:28ba]
```

QEMU confirmed attachment:

```text
Bus 7, device 0, function 0:
  3D controller: PCI device 10de:28ba
  id "hostdev0"
```

## Host Configuration

Kernel command line:

```text
vfio-pci.ids=10de:28ba
rd.driver.blacklist=nouveau
rd.driver.blacklist=nova-core
```

VFIO modules:

```text
vfio
vfio_pci
vfio_iommu_type1
```

VM hostdev XML:

```xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <driver name='vfio'/>
  <source>
    <address domain='0x0000' bus='0x01' slot='0x00' function='0x0'/>
  </source>
</hostdev>
```

## Issues Found

Available NVIDIA packages:

```text
590.48.01
```

Fedora 42 kernel:

```text
6.19.14-108.fc42.x86_64
```

Result:

```text
DKMS build failure
nvidia module not produced
nvidia-smi unable to communicate with driver
```

The same incompatibility was observed on both host and guest.

Host NVIDIA driver was previously known to work on:

```text
6.17.13-200.fc42.x86_64
```

after resolving Secure Boot / MOK signing issues.

## Conclusion

VFIO passthrough path is validated:

```text
Fedora host
  → bind GPU to vfio-pci
  → launch VM with hostdev 01:00.0
  → guest sees NVIDIA GPU
```

The remaining blocker is guest kernel/driver compatibility, not virtualization.

## Recommended Next Step

For the GitHub Actions GPU runner proof of concept:

```text
Fedora host
  → libvirt
  → ephemeral VM
  → GitHub Actions runner
  → passed-through NVIDIA GPU
```

Use a guest image with a known-good NVIDIA driver/kernel combination (or newer NVIDIA driver release) and repeat validation inside the VM.
