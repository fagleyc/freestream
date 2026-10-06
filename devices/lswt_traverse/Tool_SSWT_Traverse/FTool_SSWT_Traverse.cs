using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Data;
using System.Drawing;
using System.Linq;
using System.Text;
using System.Windows.Forms;
using System.IO;
using System.Xml;
using System.Xml.Serialization;
using Core;
using FieldTalk.Modbus.Master;

namespace Tool_SSWT_Traverse
{
  public partial class FTool_SSWT_Traverse : FTool
  {
    #region Constructors and Configuration

    private XToolSswtTraverse xToolSswtTraverse { get { return base.ixTool as XToolSswtTraverse; } }
    private XControllerTraverseSSWT xControllerTraverseSSWT { get { return (xToolSswtTraverse.XControllerMonitors[0]) as XControllerTraverseSSWT; } }
    private UControllerTraverseSSWT uControllerTraverseSSWT { get { return iControllerList[0] as UControllerTraverseSSWT; } }

    public FTool_SSWT_Traverse()
    {
      base.ixTool = new XToolSswtTraverse();

      InitializeComponent();

      initializeFTool();
      this.Text = "Sub-Sonic Wind Tunnel Traverse";

      setXConfig();
    }

    public FTool_SSWT_Traverse(string[] args)
    {
      //MessageBox.Show(this, "Tool_SSWT_Traverse:FTool_SSWT_Traverse() - exit when ready to continue.\r\nThis is to allow connection to the debugger if desired.");

      base.ixTool = new XToolSswtTraverse(); //make sure that base points to the correct class
      InitializeComponent();

      initializeFTool();
      establishTcpUdpConnection(args);

      this.Text = "Sub-Sonic Wind Tunnel TRAVERSE";

      setXConfig();
    }

    /// <summary>
    /// get the state of the UI into xTool
    /// </summary>
    protected override void getXConfig()
    {
      //base.getXConfig(); don't get the base config for this class since it wipes out the ConMonList and crashes the app

      xToolSswtTraverse.XControllerMonitors[0] = (XController)uControllerTraverseSSWT.xConfigGet();

      xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC = uIpTraverse_X_PlcIpAddressPort.XipAddressPort;
    }

    /// <summary>
    /// set the UI for this class based on the current value of xTool
    /// </summary>
    protected override void setXConfig()
    {
      if (base.ixTool == null)
      {
        MessageBox.Show(this, "Tool_SSWT_Traverse.setXConfig(): NULL base.xTool - setXConfig() FAILED");
        return;
      }

      base.setXConfig();

      if (xToolSswtTraverse != null)
      {
        txtBxUserAppName.Text = ixTool.ToolExecutableName;
        txtBxUserAppPath.Text = ixTool.ToolExecutablePath;

        uIpTraverse_X_PlcIpAddressPort.XipAddressPort = xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC;
      }

      settingConfig = true;
      ckBx_X_Axial_Enabled.Checked = false;
      ckBx_Y_Lateral_Enabled.Checked = false;
      ckBx_Z_Vertical_Enabled.Checked = false;
      //set the 3 axis check boxes
      switch(xControllerTraverseSSWT.TraverseAxisType)
      {
        case TraverseAxisTypes.X:
          ckBx_X_Axial_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.X_Y:
          ckBx_X_Axial_Enabled.Checked = true;
          ckBx_Y_Lateral_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.X_Y_Z:
          ckBx_X_Axial_Enabled.Checked = true;
          ckBx_Y_Lateral_Enabled.Checked = true;
          ckBx_Z_Vertical_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.X_Z:
          ckBx_X_Axial_Enabled.Checked = true;
          ckBx_Z_Vertical_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.Y:
          ckBx_Y_Lateral_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.Y_Z:
          ckBx_Y_Lateral_Enabled.Checked = true;
          ckBx_Z_Vertical_Enabled.Checked = true;
          break;
        case TraverseAxisTypes.Z:
          ckBx_Z_Vertical_Enabled.Checked = true;
          break;
      }
      settingConfig = false;

      //if (uControllerTraverseSSWT.UpdateCurrent_Axis_Values() == false)
      //  MessageBox.Show(this, "WARNING: The SSWT TRAVERSE failed to update the current X, Y, and Z values.", "FTool_SSWT_Traverse.FTool_SSWT_Traverse(string[] args)");
    }

    protected override void initializeFTool()
    {
      base.initializeFTool();

      this.Name = xToolSswtTraverse.ToolName + " - " + xToolSswtTraverse.ToolID;

      tabPageControllersAndMonitors.Select();

      //SSWT_Initialize_Traverse_Mover();
    }

    protected override void doXml_DeSerialization(FileStream fs)
    {
      //new serializer
      XmlSerializer newSr = new XmlSerializer(typeof(XToolSswtTraverse));
      //deserialize the object
      ixTool = (XToolSswtTraverse)newSr.Deserialize(fs);
    }

    protected override void doXml_Serialization(TextWriter tr)
    {
      XmlSerializer sr = new XmlSerializer(typeof(XToolSswtTraverse));
      sr.Serialize(tr, xToolSswtTraverse);
    }

    #endregion Constructors and Configuration

    #region ITCPclientOwner Members

    protected override System.Type getConfigObjType()
    {
      return typeof(XToolSswtTraverse);
    }

    #endregion ITCPclientOwner Members

    #region IToolUSAFAControllersMethods

    int slave;

    private MbusMasterFunctions modbusProtocol_Wago = null;

    int Modbus_Traverse_Tag7_COMMAND_RegisterAddress_Wago = 12289;
    int Modbus_Traverse_tag8_RegisterAddress_HomeButton = 12290;

    int Modbus_Traverse_Tag9_ReadEncoderPositionAddress_X_Axial = 12299;
    int Modbus_Traverse_Tag10_ReadEncoderPositionAddress_Y_Lateral = 12301;
    int Modbus_Traverse_Tag11_ReadEncoderPositionAddress_Z_Vertical = 12303;

    ushort Modbus_Traverse_Cmd_X_Axial_Forward = 1;
    ushort Modbus_Traverse_Cmd_Y_Lateral_Forward = 8;
    ushort Modbus_Traverse_Cmd_Z_Vertical_Forward = 32;

    ushort Modbus_Traverse_Cmd_X_Axial_Reverse = 2;
    ushort Modbus_Traverse_Cmd_Y_Lateral_Reverse = 4;
    ushort Modbus_Traverse_Cmd_Z_Vertical_Reverse = 16;

    ushort Modbus_Traverse_STOP_Command = 0; //which stops all the axises

    /// <summary>
    /// this just attempts to get a ModBus connection to the Wago.
    /// </summary>
    /// <returns></returns>
    public override bool Traverse_Initialize()
    {
      bool connectionSuccess = true;
      try
      {
        // connect to X Wago

        if (!getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
          connectionSuccess = false;
      }
      catch (Exception e)
      {
        MessageBox.Show(this, "Traverse failed to initialize!! Could not connect to X Wago.\r\n" + e.Message, "SSWT TRAVERSE - FTool_SSWT_Traverse.SSWT_Initialize_Sting_Mover()");
        connectionSuccess = false;
      }

      if (connectionSuccess)
        DemoNoHW = false;
      else
        DemoNoHW = true;
      return connectionSuccess;
    }

    private bool getModbusConnection(ref MbusMasterFunctions modbusConnection, string ipAddress, int tcpPort)
    {
      slave = xToolSswtTraverse.ModbusSlave;      //
      // First we must instantiate class if we haven't done so already
      //
      if ((modbusConnection == null))
      {
        try
        {
          modbusConnection = new MbusTcpMasterProtocol();
        }
        catch (OutOfMemoryException ex)
        {
          MessageBox.Show(this, "Could not connect to Wago PLC! Error was " + ex.Message, "ERROR! FTool_SSWT_Traverse.getmodbusConnection()");
          return false;
        }
      }
      else // already instantiated, close protocol and reinstantiate
      {
        if (modbusConnection.isOpen())
          modbusConnection.closeProtocol();
        modbusConnection = null;
        try
        {
          modbusConnection = new MbusTcpMasterProtocol();
        }
        catch (OutOfMemoryException ex)
        {
          MessageBox.Show(this, "Could not connect to Wago PLC! Error was " + ex.Message, "ERROR! FTool_SSWT_Traverse.getmodbusConnection()");
          return false;
        }
      }
      //
      // Here we configure the protocol
      //
      int res;

      modbusConnection.timeout = xToolSswtTraverse.ModbusTimeOut;
      modbusConnection.retryCnt = xToolSswtTraverse.ModbusRetryCnt;
      modbusConnection.pollDelay = xToolSswtTraverse.ModbusPollDelay;
      // Note: The following cast is required as the myProtocol object is declared
      // as the superclass of MbusTcpMasterProtocol. That way myProtocol can
      // represent different protocol types.
      ((MbusTcpMasterProtocol)modbusConnection).port = (short)tcpPort;
      res = ((MbusTcpMasterProtocol)modbusConnection).openProtocol(ipAddress);

      if (res == BusProtocolErrors.FTALK_SUCCESS)
        return true;
      else
        return false;
    }

    private object modbusLockObject = new object();

    ushort moveCommand = 0;

    public override void Traverse_Start_Moving(Traverse_Channel_Types traverseChannelType, bool moveForward)
    {
      {
        int result = 0;

        if (!(xControllerTraverseSSWT.isTraverseAxisActive(traverseChannelType))) return;

        try
        {
          if (traverseChannelType == Traverse_Channel_Types.X_Axial)
          {
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_X_Axial_Forward | Modbus_Traverse_Cmd_X_Axial_Reverse));
            if (moveForward)
              moveCommand |= Modbus_Traverse_Cmd_X_Axial_Forward;
            else
              moveCommand |= Modbus_Traverse_Cmd_X_Axial_Reverse;
          }
          else if (traverseChannelType == Traverse_Channel_Types.Y_Lateral)
          {
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_Y_Lateral_Forward | Modbus_Traverse_Cmd_Y_Lateral_Reverse));
            if (moveForward)
              moveCommand |= Modbus_Traverse_Cmd_Y_Lateral_Forward;
            else
              moveCommand |= Modbus_Traverse_Cmd_Y_Lateral_Reverse;
          }
          else
          {
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_Z_Vertical_Forward | Modbus_Traverse_Cmd_Z_Vertical_Reverse));
            if (moveForward)
              moveCommand |= Modbus_Traverse_Cmd_Z_Vertical_Forward;
            else
              moveCommand |= Modbus_Traverse_Cmd_Z_Vertical_Reverse;
          }

          lock (modbusLockObject)
          {
            if (getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
              result = modbusProtocol_Wago.writeSingleRegister(slave, Modbus_Traverse_Tag7_COMMAND_RegisterAddress_Wago, moveCommand);
          }

          if (result != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
          {
            string stingRow = "X";
            if (traverseChannelType == Traverse_Channel_Types.Y_Lateral)
              stingRow = "Y";
            else if (traverseChannelType == Traverse_Channel_Types.Z_Vertical)
              stingRow = "Z";
            MessageBox.Show(this, stingRow + "modbus ERROR # = " + result.ToString(), "FTool_SSWT_Traverse.Traverse_Start_Moving()");
          }
        }
        catch (Exception e)
        {
          MessageBox.Show(this, "Failed to apply STOP. !! exception = " + e.Message, "SSWT TRAVERSE - FTool_SSWT_Traverse.Traverse_Start_Moving()");
        }
      }
    }

    public override void Traverse_Stop_Moving(TraverseAxisTypes traverseChannelType)
    {
      {
        int result = 0;

        //if (!(xControllerTraverseSSWT.isTraverseAxisActive(traverseChannelType))) return;

        switch (traverseChannelType)
        {
          case TraverseAxisTypes.X:
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_X_Axial_Forward | Modbus_Traverse_Cmd_X_Axial_Reverse));
            break;
          case TraverseAxisTypes.Y:
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_Y_Lateral_Forward | Modbus_Traverse_Cmd_Y_Lateral_Reverse));
            break;
          case TraverseAxisTypes.Z:
            moveCommand &= (ushort)(~(Modbus_Traverse_Cmd_Z_Vertical_Forward | Modbus_Traverse_Cmd_Z_Vertical_Reverse));
            break;
          default:
            moveCommand = Modbus_Traverse_STOP_Command;
            break;
        }

        try
        {
          lock (modbusLockObject)
          {
            if (getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
              result = modbusProtocol_Wago.writeSingleRegister(slave, Modbus_Traverse_Tag7_COMMAND_RegisterAddress_Wago, moveCommand);
          }

          if (result != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
          {
            string stingRow = traverseChannelType.ToString();
            MessageBox.Show(this, stingRow + "modbus ERROR # = " + result.ToString(), "FTool_SSWT_Traverse.Traverse_Stop_Moving()");
          }
        }
        catch (Exception e)
        {
          MessageBox.Show(this, "Failed to apply STOP. !! exception = " + e.Message, "SSWT TRAVERSE - FTool_SSWT_Traverse.Traverse_Stop_Moving()");
        }
      }
    }

    public override uint Traverse_Read_Position(Traverse_Channel_Types traverseChannelType)
    {
      //short[] readVals = new short[125];
      int[] readVal = new int[1];
      int numRdRegs = 1;
      int res = 0;

      if (!(xControllerTraverseSSWT.isTraverseAxisActive(traverseChannelType))) return 0;

      try
      {
        lock (modbusLockObject)
        {
          if (traverseChannelType == Traverse_Channel_Types.X_Axial)
          {
            if (getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
              res = modbusProtocol_Wago.readInputLongInts(slave, Modbus_Traverse_Tag9_ReadEncoderPositionAddress_X_Axial, readVal, numRdRegs);
            //res = modbusProtocol_Wago.readMultipleRegisters(slave, Modbus_Traverse_Tag9_ReadEncoderPositionAddress_X_Axial, readVals, numRdRegs);
          }
          else if (traverseChannelType == Traverse_Channel_Types.Y_Lateral)
          {
            if (getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
              res = modbusProtocol_Wago.readInputLongInts(slave, Modbus_Traverse_Tag10_ReadEncoderPositionAddress_Y_Lateral, readVal, numRdRegs);
          }
          else if (traverseChannelType == Traverse_Channel_Types.Z_Vertical)
          {
            if (getModbusConnection(ref modbusProtocol_Wago, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.IpAddressSTR, xToolSswtTraverse.XIpaddressPort_Traverse_WAGO_PLC.Port))
              res = modbusProtocol_Wago.readInputLongInts(slave, Modbus_Traverse_Tag11_ReadEncoderPositionAddress_Z_Vertical, readVal, numRdRegs);
          }
        }

        if (res != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
        {
          string stingRow = "X";
          if (traverseChannelType == Traverse_Channel_Types.Y_Lateral)
            stingRow = "Y";
          else if (traverseChannelType == Traverse_Channel_Types.Z_Vertical)
            stingRow = "Z";
          MessageBox.Show(this, stingRow + "modbus ERROR # = " + res.ToString(), "FTool_SSWT_Traverse.Traverse_Read_Position()");
        }
      }
      catch (Exception e)
      {
        MessageBox.Show(this, "Failed to read " + traverseChannelType.ToString() + " Position. Exception = " + e.Message, "SSWT TRAVERSE - FTool_SSWT_Traverse.Traverse_Read_Position()");
      }

      return (uint)readVal[0];
    }

    #endregion IToolUSAFAControllersMethods

    #region private/protected Properties and Methods

    bool settingConfig = false;
    private void ckBx_X_Axial_Enabled_CheckedChanged(object sender, EventArgs e)
    {
      if (settingConfig)
        return;

      bool axisEnabled = false;

      if (ckBx_X_Axial_Enabled.Checked)
      {
        // enable / add the X Axial Axis
        axisEnabled = true;
      }

      if (axisEnabled)
      {
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            break;
          case TraverseAxisTypes.X_Y:
            break;
          case TraverseAxisTypes.X_Y_Z:
            break;
          case TraverseAxisTypes.X_Z:
            break;
          case TraverseAxisTypes.Y:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y;
            break;
          case TraverseAxisTypes.Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y_Z;
            break;
          case TraverseAxisTypes.Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Z;
            break;
        }
      }
      else
      {
        // disaable / remove the X Row
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            // BAD USER - shouldn't remove all the rows
            MessageBox.Show(this, "BAD USER - You CAN'T disable ALL the Axis'");
            return;
          case TraverseAxisTypes.X_Y:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Y;
            break;
          case TraverseAxisTypes.X_Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Y_Z;
            break;
          case TraverseAxisTypes.X_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Z;
            break;
          case TraverseAxisTypes.Y:
            break;
          case TraverseAxisTypes.Y_Z:
            break;
          case TraverseAxisTypes.Z:
            break;
        }
      }
      XControllerTraverse.setAxis(xControllerTraverseSSWT, xControllerTraverseSSWT.TraverseAxisType);

      uControllerTraverseSSWT.xConfigSet(xControllerTraverseSSWT);
    }

    private void ckBx_Y_Lateral_Enabled_CheckedChanged(object sender, EventArgs e)
    {
      if (settingConfig)
        return;

      bool axisEnabled = false;

      if (ckBx_Y_Lateral_Enabled.Checked)
      {
        // enable / add the Y Axial Axis
        axisEnabled = true;
      }

      if (axisEnabled)
      {
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y;
            break;
          case TraverseAxisTypes.X_Y:
            break;
          case TraverseAxisTypes.X_Y_Z:
            break;
          case TraverseAxisTypes.X_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y_Z;
            break;
          case TraverseAxisTypes.Y:
            break;
          case TraverseAxisTypes.Y_Z:
            break;
          case TraverseAxisTypes.Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Y_Z;
            break;
        }
      }
      else
      {
        // disaable / remove the Y Row
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            break;
          case TraverseAxisTypes.X_Y:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X;
            break;
          case TraverseAxisTypes.X_Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Z;
            break;
          case TraverseAxisTypes.X_Z:
            break;
          case TraverseAxisTypes.Y:
            // BAD USER - shouldn't remove all the rows
            MessageBox.Show(this, "BAD USER - You CAN'T disable ALL the Axis'");
            return;
          case TraverseAxisTypes.Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Z;
            break;
          case TraverseAxisTypes.Z:
            break;
        }
      }
      XControllerTraverse.setAxis(xControllerTraverseSSWT, xControllerTraverseSSWT.TraverseAxisType);

      uControllerTraverseSSWT.xConfigSet(xControllerTraverseSSWT);
    }

    private void ckBx_Z_Vertical_Enabled_CheckedChanged(object sender, EventArgs e)
    {
      if (settingConfig)
        return;

      bool axisEnabled = false;

      if (ckBx_Y_Lateral_Enabled.Checked)
      {
        // enable / add the Z Axial Axis
        axisEnabled = true;
      }

      if (axisEnabled)
      {
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Z;
            break;
          case TraverseAxisTypes.X_Y:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y_Z;
            break;
          case TraverseAxisTypes.X_Y_Z:
            break;
          case TraverseAxisTypes.X_Z:
            break;
          case TraverseAxisTypes.Y:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Y_Z;
            break;
          case TraverseAxisTypes.Y_Z:
            break;
          case TraverseAxisTypes.Z:
            break;
        }
      }
      else
      {
        // disaable / remove the Z Row
        switch (xControllerTraverseSSWT.TraverseAxisType)
        {
          case TraverseAxisTypes.X:
            break;
          case TraverseAxisTypes.X_Y:
            break;
          case TraverseAxisTypes.X_Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X_Y;
            break;
          case TraverseAxisTypes.X_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.X;
            break;
          case TraverseAxisTypes.Y:
            return;
          case TraverseAxisTypes.Y_Z:
            xControllerTraverseSSWT.TraverseAxisType = TraverseAxisTypes.Y;
            break;
          case TraverseAxisTypes.Z:
            // BAD USER - shouldn't remove all the rows
            MessageBox.Show(this, "BAD USER - You CAN'T disable ALL the Axis'");
            break;
        }
      }
      XControllerTraverse.setAxis(xControllerTraverseSSWT, xControllerTraverseSSWT.TraverseAxisType);

      uControllerTraverseSSWT.xConfigSet(xControllerTraverseSSWT);
    }
    #endregion private/protected Properties and Methods
  }
}
