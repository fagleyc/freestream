# ARCHIVED (2026-07)

This PLC-side homing scaffolding (FB_AxisHoming POU, %MW22+ register
blocks, commissioning docs) was **never downloaded to the WAGO** and is
superseded. Homing is now **host-side**: the rig's limit switches work
again (inverted/fixed at the module) and are exposed on StatusWord %MW1
(bit0 = X/Axial, bit1 = Y/Lateral, bit2 = Z/Vertical — negative-direction
switches), and the module hardware-limit lockout is unlinked
(`Ptr_LimitSwitch = 0`), so the host watches the StatusWord bit and
drops the jog itself. See `traverse_swt/device.py::home_axis` and the
README's homing section.

Kept for reference only — do not download.
