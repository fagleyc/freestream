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

namespace Tool_SSWT_Sting
{
  public enum Sting_Drive_Step_Level { Step_1st, Step_2nd, Step_3rd, Step_4th, Step_5th }

  public partial class FTool_SSWT_Sting : Core.FTool
  {
    #region Constructors and Configuration
    
    private XToolSswtSting xToolSswtSting { get { return base.ixTool as XToolSswtSting; } }
    private XControllerStingSSWT xControllerStingSSWT { get { return (xToolSswtSting.XControllerMonitors[0]) as XControllerStingSSWT; } }
    private UControllerStingSSWT uControllerStingSSWT { get { return iControllerList[0] as UControllerStingSSWT; } }

    public FTool_SSWT_Sting()
    {
      base.ixTool = new XToolSswtSting(); //make sure that base points to the correct class

      InitializeComponent();

      initializeFTool();
      this.Text = "Sub-Sonic Wind Tunnel Sting";

      setXConfig();
    }

    public FTool_SSWT_Sting(string[] args)
    {
      //MessageBox.Show(this, "Tool_SSWT_Sting:Tool_SSWT_Sting() - exit when ready to continue.\r\nThis is to allow connection to the debugger if desired.");

      base.ixTool = new XToolSswtSting(); //make sure that base points to the correct class
      InitializeComponent();

      initializeFTool();
      establishTcpUdpConnection(args);

      this.Text = "Sub-Sonic Wind Tunnel STING";

      setXConfig();

      //if (uControllerStingSSWT.UpdateCurrentAlphaBetaValues() == false)
      //  MessageBox.Show(this, "WARNING: The SSWT Sting failed to update the current Aplha and Beta values.", "FTool_SSWT_Sting.FTool_SSWT_Sting(string[] args)");
    }

    /// <summary>
    /// get the state of the UI into xTool
    /// </summary>
    protected override void getXConfig()
    {
      //base.getXConfig(); don't get the base config for this class since it wipes out the ConMonList and crashes the app

      xToolSswtSting.XControllerMonitors[0] = (XController)uControllerStingSSWT.xConfigGet();

      xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC = uIpStingAlphaPlcIpAddressPort.XipAddressPort;
      xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC = uIpStingBetaPlcIpAddressPort.XipAddressPort;
    }

    /// <summary>
    /// set the UI for this class based on the current value of xTool
    /// </summary>
    protected override void setXConfig()
    {
      if (base.ixTool == null)
      {
        MessageBox.Show(this, "Tool_SSWT_Sting.setXConfig(): NULL base.xTool - setXConfig() FAILED");
        return;
      }

      base.setXConfig();

      if (xToolSswtSting != null)
      {
        txtBxUserAppName.Text = ixTool.ToolExecutableName;
        txtBxUserAppPath.Text = ixTool.ToolExecutablePath;

        uIpStingAlphaPlcIpAddressPort.XipAddressPort = xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC;
        uIpStingBetaPlcIpAddressPort.XipAddressPort = xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC;
      }
      //uControllerStingSSWT.OpenSswtStingCalibrationFile();

      if (uControllerStingSSWT.UpdateCurrentAlphaBetaValues() == false)
        MessageBox.Show(this, "WARNING: The SSWT Sting failed to update the current Aplha and Beta values.", "FTool_SSWT_Sting.FTool_SSWT_Sting(string[] args)");
    }

    protected override void initializeFTool()
    {
      base.initializeFTool();

      this.Name = xToolSswtSting.ToolName + " - " + xToolSswtSting.ToolID;

      tabPageControllersAndMonitors.Select();

      //SSWT_Initialize_Sting_Mover();
    }

    protected override void doXml_DeSerialization(FileStream fs)
    {
      //new serializer
      XmlSerializer newSr = new XmlSerializer(typeof(XToolSswtSting));
      //deserialize the object
      ixTool = (XToolSswtSting)newSr.Deserialize(fs);
    }

    protected override void doXml_Serialization(TextWriter tr)
    {
      XmlSerializer sr = new XmlSerializer(typeof(XToolSswtSting));
      sr.Serialize(tr, xToolSswtSting);
    }

    #endregion Constructors and Configuration

    #region ITCPclientOwner Members

    protected override System.Type getConfigObjType()
    {
      return typeof(XToolSswtSting);
    }

    #endregion ITCPclientOwner Members

    #region IToolUSAFAControllersMethods

    int slave;

    private MbusMasterFunctions modbusProtocolAlpha = null;
    private MbusMasterFunctions modbusProtocolBeta = null;

    int StingModbusReadEncoderPositionAddressAlpha = 8714;
    int StingModbusReadEncoderPositionAddressBeta = 8714;

    int StingModbusCmdRegisterAddressAlpha = 8193;
    int StingModbusCmdRegisterAddressBeta = 8193;

    short StingModbusCmdAlphaSTOP = 17; // bits 16 + 1, 32 = 2^4
    short StingModbusCmdBetaSTOP = 33;  // bits 32 + 1, 32 = 2^5

    public override bool SSWT_Initialize_Sting_Mover()
    {
      bool connectionSuccess = true;
      try
      {
        // connect to Alpha Delta C2000

        if (!getModbusConnection(ref modbusProtocolAlpha, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.Port))
          connectionSuccess = false;
      }
      catch (Exception e)
      {
        MessageBox.Show(this, "Sting failed to initialize!! Could not connect to Alpha Delta C2000.\r\n" + e.Message, "SSWT STING - FTool_SSWT_Sting.SSWT_Initialize_Sting_Mover()");
        connectionSuccess = false;
      }

      try
      {
        // connect to Beta Delta C2000
        if (!getModbusConnection(ref modbusProtocolBeta, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.Port))
          connectionSuccess = false;
      }
      catch //(Exception e)
      {
        MessageBox.Show(this, "Sting failed to initialize!! Could not connect to Beta Delta C2000.", "SSWT STING - FTool_SSWT_Sting.SSWT_Initialize_Sting_Mover()");
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
      slave = xToolSswtSting.ModbusSlave;      //
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
          MessageBox.Show(this, "Could not connect to Alphaa Delta PLC! Error was " + ex.Message, "ERROR! FTool_SSWT_Sting.getmodbusConnection()");
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
          MessageBox.Show(this, "Could not connect to Beta Delta PLC! Error was " + ex.Message, "ERROR! FTool_SSWT_Sting.getmodbusConnection()");
          return false;
        }
      }
      //
      // Here we configure the protocol
      //
      int res;

      modbusConnection.timeout = xToolSswtSting.ModbusTimeOut;
      modbusConnection.retryCnt = xToolSswtSting.ModbusRetryCnt;
      modbusConnection.pollDelay = xToolSswtSting.ModbusPollDelay;
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
    //public object ModbusLockObject { get { return modbusLockObject; } }

    public override void Sting_Set_Drive_Step_Level(StingChannel AlphaBeta, ushort stepLevel)
    {
      //lock (modbusLockObject) //protecting against the asychronous 'Stop' command
      {
        int result = 0;
        try
        {
          if (AlphaBeta == StingChannel.Alpha)
          {
            if (getModbusConnection(ref modbusProtocolAlpha, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.Port))
              result = modbusProtocolAlpha.writeSingleRegister(slave, StingModbusCmdRegisterAddressAlpha, stepLevel);
            else
              MessageBox.Show(this, "Modbus connection to ALPHA failed", "FTool_SSWT_Sting.Sting_Set_Drive_Step_Level()");
          }
          else
          {
            if (getModbusConnection(ref modbusProtocolBeta, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.Port))
              result = modbusProtocolBeta.writeSingleRegister(slave, StingModbusCmdRegisterAddressBeta, stepLevel);
            else
              MessageBox.Show(this, "Modbus connection to BETA failed", "FTool_SSWT_Sting.Sting_Set_Drive_Step_Level()");
          }
          if (result != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
          {
            string stingRow = "ALPHA";
            if (AlphaBeta == StingChannel.Beta)
              stingRow = "BETA";
            MessageBox.Show(this, stingRow + "modbus ERROR # = " + result.ToString(), "FTool_SSWT_Sting.Sting_Set_Drive_Step_Level()");
          }
        }
        catch
        {
          MessageBox.Show(this, "STING PLC Modbus failed to set voltage out!!", "SSWT STING - FTool_SSWT_Sting.Sting_Apply_Voltage()");
        }
      }
    }

    /// <summary>
    /// re-proposing to set the digital start move channel as necessary
    /// </summary>
    /// <param name="Brake_on">Should be Start_Moving. TRUE = START MOVING</param>
    /// <param name="AlphaBeta"></param>
    public override void Sting_Apply_Brake(bool Brake_on, StingChannel AlphaBeta)
    {
      //lock (modbusLockObject) //protecting against the asychronous 'Stop' command
      {
        short[] writeVals = new short[1];
        int result = 0;

        try
        {
          if (Brake_on)
            if (AlphaBeta == StingChannel.Alpha)
            {
              if (getModbusConnection(ref modbusProtocolAlpha, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.Port))
                result = modbusProtocolAlpha.writeSingleRegister(slave, StingModbusCmdRegisterAddressAlpha, StingModbusCmdAlphaSTOP);
            }
            else
            {
              if (getModbusConnection(ref modbusProtocolBeta, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.Port))
                result = modbusProtocolBeta.writeSingleRegister(slave, StingModbusCmdRegisterAddressBeta, StingModbusCmdBetaSTOP);
            }
          if (result != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
          {
            string stingRow = "ALPHA";
            if (AlphaBeta == StingChannel.Beta)
              stingRow = "BETA";
            MessageBox.Show(this, stingRow + "modbus ERROR # = " + result.ToString(), "FTool_SSWT_Sting.Sting_Apply_Brake()");
          }
        }
        catch (Exception e)
        {
          MessageBox.Show(this, "Failed to apply break setting. !! exception = " + e.Message, "SSWT STING - FTool_SSWT_Sting.Sting_Apply_Brake()");
        }
      }
    }

    /// <summary>
    /// re-purposing this method to return the value of the position
    /// </summary>
    /// <param name="AlphaBetaChannelType"></param>
    /// <returns></returns>
    public override float Sting_Read_Position_Voltage(StingChannelTypes AlphaBetaChannelType)
    {
      //lock (modbusLockObject) //protecting against the asychronous 'Stop' command
      {
        short[] readVals = new short[125];
        int numRdRegs = 1;
        int res = 0;

        try
        {
          if (AlphaBetaChannelType == StingChannelTypes.Alpha)
          {
            if (getModbusConnection(ref modbusProtocolAlpha, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingALPHA_DeltaC2000PLC.Port))
              res = modbusProtocolAlpha.readMultipleRegisters(slave, StingModbusReadEncoderPositionAddressAlpha, readVals, numRdRegs);
          }
          else
          {
            if (getModbusConnection(ref modbusProtocolBeta, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.IpAddressSTR, xToolSswtSting.XIpaddressPort_StingBETA_DeltaC2000PLC.Port))
              res = modbusProtocolBeta.readMultipleRegisters(slave, StingModbusReadEncoderPositionAddressBeta, readVals, numRdRegs);
          }
          if (res != FieldTalk.Modbus.Master.BusProtocolErrors.FTALK_SUCCESS)
          {
            string stingRow = "ALPHA";
            if (AlphaBetaChannelType == StingChannelTypes.Alpha)
              stingRow = "BETA";
            MessageBox.Show(this, stingRow + "modbus ERROR # = " + res.ToString(), "FTool_SSWT_Sting.Sting_Apply_Brake()");
          }
        }
        catch (Exception e)
        {
          MessageBox.Show(this, "Failed to read " + AlphaBetaChannelType.ToString() + " Position. Exception = " + e.Message, "SSWT STING - FTool_SSWT_Sting.Sting_Read_Position_Voltage()");
        }

        return readVals[0];
      }
    }

    #endregion IToolUSAFAControllersMethods
  }
}
