namespace Tool_LSWT_Sting
{
  partial class FTool_LSWT_Sting
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
      this.lblCommPort = new System.Windows.Forms.Label();
      this.txBxCommPortName = new System.Windows.Forms.TextBox();
      this.ckBxEnableAlpha = new System.Windows.Forms.CheckBox();
      this.ckBxEnableBeta = new System.Windows.Forms.CheckBox();
      ((System.ComponentModel.ISupportInitialize)(this.splitContainer1)).BeginInit();
      this.splitContainer1.Panel1.SuspendLayout();
      this.splitContainer1.Panel2.SuspendLayout();
      this.splitContainer1.SuspendLayout();
      this.tabControl1.SuspendLayout();
      this.tabPageDevices.SuspendLayout();
      this.SuspendLayout();
      // 
      // splitContainer1
      // 
      // 
      // tabPageDevices
      // 
      this.tabPageDevices.Controls.Add(this.ckBxEnableBeta);
      this.tabPageDevices.Controls.Add(this.ckBxEnableAlpha);
      this.tabPageDevices.Controls.Add(this.txBxCommPortName);
      this.tabPageDevices.Controls.Add(this.lblCommPort);
      // 
      // lblCommPort
      // 
      this.lblCommPort.AutoSize = true;
      this.lblCommPort.Location = new System.Drawing.Point(25, 24);
      this.lblCommPort.Name = "lblCommPort";
      this.lblCommPort.Size = new System.Drawing.Size(150, 13);
      this.lblCommPort.TabIndex = 0;
      this.lblCommPort.Text = "Comm Port to control the Sting";
      // 
      // txBxCommPortName
      // 
      this.txBxCommPortName.Location = new System.Drawing.Point(181, 21);
      this.txBxCommPortName.Name = "txBxCommPortName";
      this.txBxCommPortName.Size = new System.Drawing.Size(100, 20);
      this.txBxCommPortName.TabIndex = 1;
      this.txBxCommPortName.KeyDown += new System.Windows.Forms.KeyEventHandler(this.txBxCommPortName_KeyDown);
      this.txBxCommPortName.Leave += new System.EventHandler(this.txBxCommPortName_Leave);
      // 
      // ckBxEnableAlpha
      // 
      this.ckBxEnableAlpha.AutoSize = true;
      this.ckBxEnableAlpha.Checked = true;
      this.ckBxEnableAlpha.CheckState = System.Windows.Forms.CheckState.Checked;
      this.ckBxEnableAlpha.Location = new System.Drawing.Point(28, 62);
      this.ckBxEnableAlpha.Name = "ckBxEnableAlpha";
      this.ckBxEnableAlpha.Size = new System.Drawing.Size(89, 17);
      this.ckBxEnableAlpha.TabIndex = 2;
      this.ckBxEnableAlpha.Text = "Enable Alpha";
      this.ckBxEnableAlpha.UseVisualStyleBackColor = true;
      this.ckBxEnableAlpha.CheckedChanged += new System.EventHandler(this.ckBxEnableAlpha_CheckedChanged);
      // 
      // ckBxEnableBeta
      // 
      this.ckBxEnableBeta.AutoSize = true;
      this.ckBxEnableBeta.Checked = true;
      this.ckBxEnableBeta.CheckState = System.Windows.Forms.CheckState.Checked;
      this.ckBxEnableBeta.Location = new System.Drawing.Point(28, 85);
      this.ckBxEnableBeta.Name = "ckBxEnableBeta";
      this.ckBxEnableBeta.Size = new System.Drawing.Size(84, 17);
      this.ckBxEnableBeta.TabIndex = 3;
      this.ckBxEnableBeta.Text = "Enable Beta";
      this.ckBxEnableBeta.UseVisualStyleBackColor = true;
      this.ckBxEnableBeta.CheckedChanged += new System.EventHandler(this.ckBxEnableBeta_CheckedChanged);
      // 
      // FTool_LSWT_Sting
      // 
      this.AutoScaleDimensions = new System.Drawing.SizeF(6F, 13F);
      this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
      this.ClientSize = new System.Drawing.Size(1177, 458);
      this.Name = "FTool_LSWT_Sting";
      this.Text = "FTool_LSWT_Sting";
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

    private System.Windows.Forms.Label lblCommPort;
    private System.Windows.Forms.TextBox txBxCommPortName;
    private System.Windows.Forms.CheckBox ckBxEnableBeta;
    private System.Windows.Forms.CheckBox ckBxEnableAlpha;
  }
}