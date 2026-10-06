namespace Tool_SSWT_Traverse
{
  partial class FTool_SSWT_Traverse
  {
    /// <summary>
    /// Required designer variable.
    /// </summary>
    private System.ComponentModel.IContainer components = null;

    /// <summary>
    /// Clean up any resources being used.
    /// </summary>
    /// <param name="disposing">true if managed resources should be disposed; otherwise, false.</param>
    protected override void Dispose(bool disposing)
    {
      if (disposing && (components != null))
      {
        components.Dispose();
      }
      base.Dispose(disposing);
    }

    #region Windows Form Designer generated code

    /// <summary>
    /// Required method for Designer support - do not modify
    /// the contents of this method with the code editor.
    /// </summary>
    private void InitializeComponent()
    {
      System.ComponentModel.ComponentResourceManager resources = new System.ComponentModel.ComponentResourceManager(typeof(FTool_SSWT_Traverse));
      Core.XIpAddressPort xIpAddressPort1 = new Core.XIpAddressPort();
      this.txtBxUserAppPath = new System.Windows.Forms.TextBox();
      this.label4 = new System.Windows.Forms.Label();
      this.txtBxUserAppName = new System.Windows.Forms.TextBox();
      this.label3 = new System.Windows.Forms.Label();
      this.lblTraverse_X_DeltaC2000PlcIpPort = new System.Windows.Forms.Label();
      this.uIpTraverse_X_PlcIpAddressPort = new Core.UIpAddressPort();
      this.ckBx_X_Axial_Enabled = new System.Windows.Forms.CheckBox();
      this.ckBx_Y_Lateral_Enabled = new System.Windows.Forms.CheckBox();
      this.ckBx_Z_Vertical_Enabled = new System.Windows.Forms.CheckBox();
      this.lblModbusDelay = new System.Windows.Forms.Label();
      this.txtBxRxIntModbusTimeout = new Core.TxtBxRxInt();
      this.lblModbusTimeout = new System.Windows.Forms.Label();
      this.txtBxRxIntModbusSlave = new Core.TxtBxRxInt();
      this.lblModbusSlave = new System.Windows.Forms.Label();
      this.txtBxRxIntModbusRetryCount = new Core.TxtBxRxInt();
      this.lblModbusRetryCount = new System.Windows.Forms.Label();
      this.txtBxRxIntModbusPollDelay = new Core.TxtBxRxInt();
      this.lblModbusCommunication = new System.Windows.Forms.Label();
      ((System.ComponentModel.ISupportInitialize)(this.splitContainer1)).BeginInit();
      this.splitContainer1.Panel1.SuspendLayout();
      this.splitContainer1.Panel2.SuspendLayout();
      this.splitContainer1.SuspendLayout();
      this.tabControl1.SuspendLayout();
      this.tabPageDevices.SuspendLayout();
      this.SuspendLayout();
      // 
      // uStatusMsg
      // 
      this.uStatusMsg.Margin = new System.Windows.Forms.Padding(5);
      this.uStatusMsg.Size = new System.Drawing.Size(787, 43);
      // 
      // splitContainer1
      // 
      this.splitContainer1.Margin = new System.Windows.Forms.Padding(4);
      this.splitContainer1.Size = new System.Drawing.Size(787, 593);
      this.splitContainer1.SplitterDistance = 545;
      this.splitContainer1.SplitterWidth = 5;
      // 
      // tabControl1
      // 
      this.tabControl1.Margin = new System.Windows.Forms.Padding(4);
      this.tabControl1.Size = new System.Drawing.Size(787, 545);
      // 
      // tabPageDevices
      // 
      this.tabPageDevices.Controls.Add(this.lblModbusDelay);
      this.tabPageDevices.Controls.Add(this.txtBxRxIntModbusTimeout);
      this.tabPageDevices.Controls.Add(this.lblModbusTimeout);
      this.tabPageDevices.Controls.Add(this.txtBxRxIntModbusSlave);
      this.tabPageDevices.Controls.Add(this.lblModbusSlave);
      this.tabPageDevices.Controls.Add(this.txtBxRxIntModbusRetryCount);
      this.tabPageDevices.Controls.Add(this.lblModbusRetryCount);
      this.tabPageDevices.Controls.Add(this.txtBxRxIntModbusPollDelay);
      this.tabPageDevices.Controls.Add(this.lblModbusCommunication);
      this.tabPageDevices.Controls.Add(this.ckBx_Z_Vertical_Enabled);
      this.tabPageDevices.Controls.Add(this.ckBx_Y_Lateral_Enabled);
      this.tabPageDevices.Controls.Add(this.ckBx_X_Axial_Enabled);
      this.tabPageDevices.Controls.Add(this.txtBxUserAppPath);
      this.tabPageDevices.Controls.Add(this.label4);
      this.tabPageDevices.Controls.Add(this.txtBxUserAppName);
      this.tabPageDevices.Controls.Add(this.label3);
      this.tabPageDevices.Controls.Add(this.lblTraverse_X_DeltaC2000PlcIpPort);
      this.tabPageDevices.Controls.Add(this.uIpTraverse_X_PlcIpAddressPort);
      this.tabPageDevices.Margin = new System.Windows.Forms.Padding(4);
      this.tabPageDevices.Padding = new System.Windows.Forms.Padding(4);
      this.tabPageDevices.Size = new System.Drawing.Size(779, 519);
      // 
      // tabPageControllersAndMonitors
      // 
      this.tabPageControllersAndMonitors.Margin = new System.Windows.Forms.Padding(4);
      this.tabPageControllersAndMonitors.Padding = new System.Windows.Forms.Padding(4);
      this.tabPageControllersAndMonitors.Size = new System.Drawing.Size(1169, 337);
      // 
      // txtBxUserAppPath
      // 
      this.txtBxUserAppPath.Enabled = false;
      this.txtBxUserAppPath.Location = new System.Drawing.Point(146, 55);
      this.txtBxUserAppPath.Name = "txtBxUserAppPath";
      this.txtBxUserAppPath.ReadOnly = true;
      this.txtBxUserAppPath.Size = new System.Drawing.Size(534, 20);
      this.txtBxUserAppPath.TabIndex = 253;
      // 
      // label4
      // 
      this.label4.AutoSize = true;
      this.label4.Location = new System.Drawing.Point(31, 58);
      this.label4.Name = "label4";
      this.label4.Size = new System.Drawing.Size(109, 13);
      this.label4.TabIndex = 254;
      this.label4.Text = "User Application Path";
      // 
      // txtBxUserAppName
      // 
      this.txtBxUserAppName.Enabled = false;
      this.txtBxUserAppName.Location = new System.Drawing.Point(146, 16);
      this.txtBxUserAppName.Name = "txtBxUserAppName";
      this.txtBxUserAppName.ReadOnly = true;
      this.txtBxUserAppName.Size = new System.Drawing.Size(393, 20);
      this.txtBxUserAppName.TabIndex = 251;
      // 
      // label3
      // 
      this.label3.AutoSize = true;
      this.label3.Location = new System.Drawing.Point(25, 19);
      this.label3.Name = "label3";
      this.label3.Size = new System.Drawing.Size(115, 13);
      this.label3.TabIndex = 252;
      this.label3.Text = "User Application Name";
      // 
      // lblTraverse_X_DeltaC2000PlcIpPort
      // 
      this.lblTraverse_X_DeltaC2000PlcIpPort.AutoSize = true;
      this.lblTraverse_X_DeltaC2000PlcIpPort.Font = new System.Drawing.Font("Microsoft Sans Serif", 8.25F, System.Drawing.FontStyle.Bold, System.Drawing.GraphicsUnit.Point, ((byte)(0)));
      this.lblTraverse_X_DeltaC2000PlcIpPort.Location = new System.Drawing.Point(27, 145);
      this.lblTraverse_X_DeltaC2000PlcIpPort.Name = "lblTraverse_X_DeltaC2000PlcIpPort";
      this.lblTraverse_X_DeltaC2000PlcIpPort.Size = new System.Drawing.Size(210, 13);
      this.lblTraverse_X_DeltaC2000PlcIpPort.TabIndex = 248;
      this.lblTraverse_X_DeltaC2000PlcIpPort.Text = "TRAVERSE WAGO PLC IP and Port";
      // 
      // uIpTraverse_X_PlcIpAddressPort
      // 
      this.uIpTraverse_X_PlcIpAddressPort.AutoSize = true;
      this.uIpTraverse_X_PlcIpAddressPort.IpAddress = ((System.Net.IPAddress)(resources.GetObject("uIpTraverse_X_PlcIpAddressPort.IpAddress")));
      this.uIpTraverse_X_PlcIpAddressPort.Location = new System.Drawing.Point(28, 161);
      this.uIpTraverse_X_PlcIpAddressPort.Margin = new System.Windows.Forms.Padding(4);
      this.uIpTraverse_X_PlcIpAddressPort.Name = "uIpTraverse_X_PlcIpAddressPort";
      this.uIpTraverse_X_PlcIpAddressPort.Port = 502;
      this.uIpTraverse_X_PlcIpAddressPort.PortNumbMaxValue = 69999;
      this.uIpTraverse_X_PlcIpAddressPort.PortNumbMinValue = 0;
      this.uIpTraverse_X_PlcIpAddressPort.ReadOnly = false;
      this.uIpTraverse_X_PlcIpAddressPort.Size = new System.Drawing.Size(431, 27);
      this.uIpTraverse_X_PlcIpAddressPort.TabIndex = 247;
      this.toolTip1.SetToolTip(this.uIpTraverse_X_PlcIpAddressPort, "The IP Address / TCP Port of the machine running the HDF5 server");
      xIpAddressPort1.IpAddress = ((System.Net.IPAddress)(resources.GetObject("xIpAddressPort1.IpAddress")));
      xIpAddressPort1.IpAddressSTR = "127.0.0.1";
      xIpAddressPort1.Port = 502;
      this.uIpTraverse_X_PlcIpAddressPort.XipAddressPort = xIpAddressPort1;
      // 
      // ckBx_X_Axial_Enabled
      // 
      this.ckBx_X_Axial_Enabled.AutoSize = true;
      this.ckBx_X_Axial_Enabled.Checked = true;
      this.ckBx_X_Axial_Enabled.CheckState = System.Windows.Forms.CheckState.Checked;
      this.ckBx_X_Axial_Enabled.Location = new System.Drawing.Point(28, 106);
      this.ckBx_X_Axial_Enabled.Name = "ckBx_X_Axial_Enabled";
      this.ckBx_X_Axial_Enabled.Size = new System.Drawing.Size(100, 17);
      this.ckBx_X_Axial_Enabled.TabIndex = 255;
      this.ckBx_X_Axial_Enabled.Text = "X Axial Enabled";
      this.ckBx_X_Axial_Enabled.UseVisualStyleBackColor = true;
      this.ckBx_X_Axial_Enabled.CheckedChanged += new System.EventHandler(this.ckBx_X_Axial_Enabled_CheckedChanged);
      // 
      // ckBx_Y_Lateral_Enabled
      // 
      this.ckBx_Y_Lateral_Enabled.AutoSize = true;
      this.ckBx_Y_Lateral_Enabled.Checked = true;
      this.ckBx_Y_Lateral_Enabled.CheckState = System.Windows.Forms.CheckState.Checked;
      this.ckBx_Y_Lateral_Enabled.Location = new System.Drawing.Point(164, 106);
      this.ckBx_Y_Lateral_Enabled.Name = "ckBx_Y_Lateral_Enabled";
      this.ckBx_Y_Lateral_Enabled.Size = new System.Drawing.Size(110, 17);
      this.ckBx_Y_Lateral_Enabled.TabIndex = 256;
      this.ckBx_Y_Lateral_Enabled.Text = "Y Lateral Enabled";
      this.ckBx_Y_Lateral_Enabled.UseVisualStyleBackColor = true;
      this.ckBx_Y_Lateral_Enabled.CheckedChanged += new System.EventHandler(this.ckBx_Y_Lateral_Enabled_CheckedChanged);
      // 
      // ckBx_Z_Vertical_Enabled
      // 
      this.ckBx_Z_Vertical_Enabled.AutoSize = true;
      this.ckBx_Z_Vertical_Enabled.Checked = true;
      this.ckBx_Z_Vertical_Enabled.CheckState = System.Windows.Forms.CheckState.Checked;
      this.ckBx_Z_Vertical_Enabled.Location = new System.Drawing.Point(298, 106);
      this.ckBx_Z_Vertical_Enabled.Name = "ckBx_Z_Vertical_Enabled";
      this.ckBx_Z_Vertical_Enabled.Size = new System.Drawing.Size(113, 17);
      this.ckBx_Z_Vertical_Enabled.TabIndex = 257;
      this.ckBx_Z_Vertical_Enabled.Text = "Z Vertical Enabled";
      this.ckBx_Z_Vertical_Enabled.UseVisualStyleBackColor = true;
      this.ckBx_Z_Vertical_Enabled.CheckedChanged += new System.EventHandler(this.ckBx_Z_Vertical_Enabled_CheckedChanged);
      // 
      // lblModbusDelay
      // 
      this.lblModbusDelay.AutoSize = true;
      this.lblModbusDelay.Location = new System.Drawing.Point(54, 237);
      this.lblModbusDelay.Name = "lblModbusDelay";
      this.lblModbusDelay.Size = new System.Drawing.Size(34, 13);
      this.lblModbusDelay.TabIndex = 266;
      this.lblModbusDelay.Text = "Delay";
      // 
      // txtBxRxIntModbusTimeout
      // 
      this.txtBxRxIntModbusTimeout.CurrentValue = 1000;
      this.txtBxRxIntModbusTimeout.DuringUnitsChange = false;
      this.txtBxRxIntModbusTimeout.FormatStr = "d2";
      this.txtBxRxIntModbusTimeout.Location = new System.Drawing.Point(267, 260);
      this.txtBxRxIntModbusTimeout.MaxValue = 0;
      this.txtBxRxIntModbusTimeout.MinValue = 0;
      this.txtBxRxIntModbusTimeout.Name = "txtBxRxIntModbusTimeout";
      this.txtBxRxIntModbusTimeout.RegexStr = "-?\\d*";
      this.txtBxRxIntModbusTimeout.Size = new System.Drawing.Size(100, 20);
      this.txtBxRxIntModbusTimeout.TabIndex = 265;
      this.txtBxRxIntModbusTimeout.Text = "1000";
      // 
      // lblModbusTimeout
      // 
      this.lblModbusTimeout.AutoSize = true;
      this.lblModbusTimeout.Location = new System.Drawing.Point(216, 263);
      this.lblModbusTimeout.Name = "lblModbusTimeout";
      this.lblModbusTimeout.Size = new System.Drawing.Size(45, 13);
      this.lblModbusTimeout.TabIndex = 264;
      this.lblModbusTimeout.Text = "Timeout";
      // 
      // txtBxRxIntModbusSlave
      // 
      this.txtBxRxIntModbusSlave.CurrentValue = 1;
      this.txtBxRxIntModbusSlave.DuringUnitsChange = false;
      this.txtBxRxIntModbusSlave.FormatStr = "d";
      this.txtBxRxIntModbusSlave.Location = new System.Drawing.Point(267, 234);
      this.txtBxRxIntModbusSlave.MaxValue = 0;
      this.txtBxRxIntModbusSlave.MinValue = 0;
      this.txtBxRxIntModbusSlave.Name = "txtBxRxIntModbusSlave";
      this.txtBxRxIntModbusSlave.RegexStr = "-?\\d*";
      this.txtBxRxIntModbusSlave.Size = new System.Drawing.Size(100, 20);
      this.txtBxRxIntModbusSlave.TabIndex = 263;
      this.txtBxRxIntModbusSlave.Text = "01";
      // 
      // lblModbusSlave
      // 
      this.lblModbusSlave.AutoSize = true;
      this.lblModbusSlave.Location = new System.Drawing.Point(217, 237);
      this.lblModbusSlave.Name = "lblModbusSlave";
      this.lblModbusSlave.Size = new System.Drawing.Size(44, 13);
      this.lblModbusSlave.TabIndex = 262;
      this.lblModbusSlave.Text = "Slave #";
      // 
      // txtBxRxIntModbusRetryCount
      // 
      this.txtBxRxIntModbusRetryCount.CurrentValue = 0;
      this.txtBxRxIntModbusRetryCount.DuringUnitsChange = false;
      this.txtBxRxIntModbusRetryCount.FormatStr = "d";
      this.txtBxRxIntModbusRetryCount.Location = new System.Drawing.Point(94, 260);
      this.txtBxRxIntModbusRetryCount.MaxValue = 0;
      this.txtBxRxIntModbusRetryCount.MinValue = 0;
      this.txtBxRxIntModbusRetryCount.Name = "txtBxRxIntModbusRetryCount";
      this.txtBxRxIntModbusRetryCount.RegexStr = "-?\\d*";
      this.txtBxRxIntModbusRetryCount.Size = new System.Drawing.Size(100, 20);
      this.txtBxRxIntModbusRetryCount.TabIndex = 261;
      this.txtBxRxIntModbusRetryCount.Text = "0";
      // 
      // lblModbusRetryCount
      // 
      this.lblModbusRetryCount.AutoSize = true;
      this.lblModbusRetryCount.Location = new System.Drawing.Point(25, 263);
      this.lblModbusRetryCount.Name = "lblModbusRetryCount";
      this.lblModbusRetryCount.Size = new System.Drawing.Size(63, 13);
      this.lblModbusRetryCount.TabIndex = 260;
      this.lblModbusRetryCount.Text = "Retry Count";
      // 
      // txtBxRxIntModbusPollDelay
      // 
      this.txtBxRxIntModbusPollDelay.CurrentValue = 0;
      this.txtBxRxIntModbusPollDelay.DuringUnitsChange = false;
      this.txtBxRxIntModbusPollDelay.FormatStr = "d";
      this.txtBxRxIntModbusPollDelay.Location = new System.Drawing.Point(94, 234);
      this.txtBxRxIntModbusPollDelay.MaxValue = 0;
      this.txtBxRxIntModbusPollDelay.MinValue = 0;
      this.txtBxRxIntModbusPollDelay.Name = "txtBxRxIntModbusPollDelay";
      this.txtBxRxIntModbusPollDelay.RegexStr = "-?\\d*";
      this.txtBxRxIntModbusPollDelay.Size = new System.Drawing.Size(100, 20);
      this.txtBxRxIntModbusPollDelay.TabIndex = 259;
      this.txtBxRxIntModbusPollDelay.Text = "0";
      // 
      // lblModbusCommunication
      // 
      this.lblModbusCommunication.AutoSize = true;
      this.lblModbusCommunication.Font = new System.Drawing.Font("Microsoft Sans Serif", 8.25F, System.Drawing.FontStyle.Bold, System.Drawing.GraphicsUnit.Point, ((byte)(0)));
      this.lblModbusCommunication.Location = new System.Drawing.Point(25, 207);
      this.lblModbusCommunication.Name = "lblModbusCommunication";
      this.lblModbusCommunication.Size = new System.Drawing.Size(140, 13);
      this.lblModbusCommunication.TabIndex = 258;
      this.lblModbusCommunication.Text = "Modbus Communication";
      // 
      // FTool_SSWT_Traverse
      // 
      this.AutoScaleDimensions = new System.Drawing.SizeF(6F, 13F);
      this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
      this.ClientSize = new System.Drawing.Size(787, 656);
      this.Margin = new System.Windows.Forms.Padding(4);
      this.Name = "FTool_SSWT_Traverse";
      this.Text = "Form1";
      this.splitContainer1.Panel1.ResumeLayout(false);
      this.splitContainer1.Panel2.ResumeLayout(false);
      ((System.ComponentModel.ISupportInitialize)(this.splitContainer1)).EndInit();
      this.splitContainer1.ResumeLayout(false);
      this.tabControl1.ResumeLayout(false);
      this.tabPageDevices.ResumeLayout(false);
      this.tabPageDevices.PerformLayout();
      this.ResumeLayout(false);
      this.PerformLayout();

    }

    #endregion

    private System.Windows.Forms.TextBox txtBxUserAppPath;
    private System.Windows.Forms.Label label4;
    private System.Windows.Forms.TextBox txtBxUserAppName;
    private System.Windows.Forms.Label label3;
    private System.Windows.Forms.Label lblTraverse_X_DeltaC2000PlcIpPort;
    private Core.UIpAddressPort uIpTraverse_X_PlcIpAddressPort;
    private System.Windows.Forms.CheckBox ckBx_Z_Vertical_Enabled;
    private System.Windows.Forms.CheckBox ckBx_Y_Lateral_Enabled;
    private System.Windows.Forms.CheckBox ckBx_X_Axial_Enabled;
    private System.Windows.Forms.Label lblModbusDelay;
    private Core.TxtBxRxInt txtBxRxIntModbusTimeout;
    private System.Windows.Forms.Label lblModbusTimeout;
    private Core.TxtBxRxInt txtBxRxIntModbusSlave;
    private System.Windows.Forms.Label lblModbusSlave;
    private Core.TxtBxRxInt txtBxRxIntModbusRetryCount;
    private System.Windows.Forms.Label lblModbusRetryCount;
    private Core.TxtBxRxInt txtBxRxIntModbusPollDelay;
    private System.Windows.Forms.Label lblModbusCommunication;
  }
}