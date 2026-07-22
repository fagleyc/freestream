"""LSWT fan-drive package — North & South Low-Speed Wind Tunnels.

Each tunnel's fan runs on an ABB ACS530 VFD ("ABB530"/"ACB530" in the
deployed C#) reached over Modbus TCP, unit 1. Protocol + calibration
were extracted from the deployed C# source
``Tool_LSWT_Flow_Velocity\\HwControllerVelocityLSWT_ACB530.cs``.
"""
