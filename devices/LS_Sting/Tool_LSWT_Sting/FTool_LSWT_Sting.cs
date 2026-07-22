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

namespace Tool_LSWT_Sting
{
  public partial class FTool_LSWT_Sting : Core.FTool
  {
    #region Constructors and Configuration
    
    private XToolLswtSting xToolLswtSting { get { return base.ixTool as XToolLswtSting; } }
    private XControllerStingLSWT xControllerStingLSWT { get { return (xToolLswtSting.XControllerMonitors[0]) as XControllerStingLSWT; } }
    private UControllerStingLSWT uControllerStingLSWT { get { return iControllerList[0] as UControllerStingLSWT; } }

    public FTool_LSWT_Sting()
    {
      base.ixTool = new XToolLswtSting(); //make sure that base points to the correct class

      InitializeComponent();

      initializeFTool();
      this.Text = "Low Speed Wind Tunnel Sting";

      setXConfig();
    }

    public FTool_LSWT_Sting(string[] args)
    {
      //MessageBox.Show("Tool_LSWT_Sting:FTool_LSWT_Sting() - exit when ready to continue.\r\nThis is to allow connection to the debugger if desired.");

      base.ixTool = new XToolLswtSting(); //make sure that base points to the correct class
      InitializeComponent();

      initializeFTool();

      this.Text = "Low Speed Wind Tunnel STING";

      setXConfig();

      establishTcpUdpConnection(args);
    }

    /// <summary>
    /// get the state of the UI into xTool
    /// </summary>
    protected override void getXConfig()
    {
      //base.getXConfig(); don't get the base config for this class since it wipes out the ConMonList and crashes the app

      xToolLswtSting.XControllerMonitors[0] = (XController)uControllerStingLSWT.xConfigGet();

      xControllerStingLSWT.Rs232ComPortName = txBxCommPortName.Text;
      xControllerStingLSWT.EnableAlpha = ckBxEnableAlpha.Checked;
      xControllerStingLSWT.EnableBeta = ckBxEnableBeta.Checked;
    }

    /// <summary>
    /// set the UI for this class based on the current value of xTool
    /// </summary>
    protected override void setXConfig()
    {
      if (base.ixTool == null)
      {
        MessageBox.Show("Tool_LSWT_Sting.setXConfig(): NULL base.xTool - setXConfig() FAILED");
        return;
      }

      if (iControllerList.Count > 0)
      {
        // remove the existing spComm
        uControllerStingLSWT.Dispose();
      }
      base.setXConfig();

      if (xToolLswtSting != null)
      {
        txBxCommPortName.Text = xControllerStingLSWT.Rs232ComPortName;
        ckBxEnableAlpha.Checked = xControllerStingLSWT.EnableAlpha;
        ckBxEnableBeta.Checked = xControllerStingLSWT.EnableBeta;
        //txtBxUserAppName.Text = ixTool.ToolExecutableName;
        //txtBxUserAppPath.Text = ixTool.ToolExecutablePath;
      }

      //if (uControllerStingLSWT.UpdateCurrentAlphaBetaValues() == false)
      //  MessageBox.Show("WARNING: The LSWT Sting failed to update the current Aplha and Beta values.", "FTool_LSWT_Sting.FTool_LSWT_Sting(string[] args)");
    }

    protected override void initializeFTool()
    {
      base.initializeFTool();

      this.Name = xToolLswtSting.ToolName + " - " + xToolLswtSting.ToolID;

      tabPageControllersAndMonitors.Select();

      //LSWT_Initialize_Sting_Mover();
    }

    protected override void doXml_DeSerialization(FileStream fs)
    {
      //new serializer
      XmlSerializer newSr = new XmlSerializer(typeof(XToolLswtSting));
      //deserialize the object
      ixTool = (XToolLswtSting)newSr.Deserialize(fs);
    }

    protected override void doXml_Serialization(TextWriter tr)
    {
      XmlSerializer sr = new XmlSerializer(typeof(XToolLswtSting));
      sr.Serialize(tr, xToolLswtSting);
    }

    #endregion Constructors and Configuration

    #region ITCPclientOwner Members

    protected override System.Type getConfigObjType()
    {
      return typeof(XToolLswtSting);
    }

    #endregion ITCPclientOwner Members

    private void txBxCommPortName_KeyDown(object sender, KeyEventArgs e)
    {
      if (e.KeyData == Keys.Enter) // 13 is the enter key
      {
        uControllerStingLSWT.ComPortName(txBxCommPortName.Text);
      }
    }

    private void txBxCommPortName_Leave(object sender, EventArgs e)
    {
      // update validate and update textBox and underlying control(s)
      uControllerStingLSWT.ComPortName(txBxCommPortName.Text);
    }

    private void ckBxEnableAlpha_CheckedChanged(object sender, EventArgs e)
    {
      xControllerStingLSWT.EnableAlpha = ckBxEnableAlpha.Checked;
      uControllerStingLSWT.EnableAlpha = xControllerStingLSWT.EnableAlpha;
    }

    private void ckBxEnableBeta_CheckedChanged(object sender, EventArgs e)
    {
      xControllerStingLSWT.EnableBeta = ckBxEnableBeta.Checked;
      uControllerStingLSWT.EnableBeta = xControllerStingLSWT.EnableBeta;
    }

  }
}
