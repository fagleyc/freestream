using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using Core;

namespace Core
{
  public class HwControllerStingSSWT : Core.HwControllerSting_HWsideAB
  {
    #region Constructors and Configuration

    public HwControllerStingSSWT(UControllerStingSSWT uControllerStingSSWT)
    {
      ready = false;
      base.IControllerUI = uControllerStingSSWT;

      this.timerServoCmndActive = new System.Windows.Forms.Timer();
      this.timerServoCmndActive.Interval = 100;  // check for movement adjustmant and completion every 100mSec
      this.timerServoCmndActive.Tick += new System.EventHandler(this.timerServoCmndActive_Tick);
      this.timerServoCmndActive.Enabled = false; //let the first use start the timer

      InitHw();

      if (demoMode)
        this.timerServoCmndActive.Interval = 1000; //don't need to go as fast for the demo
    }
    
    protected UControllerStingSSWT uControllerStingSSWT { get { return IControllerUI as UControllerStingSSWT; } set { IControllerUI = value; } }
    protected XControllerStingSSWT xControllerStingSSWT { get { return uControllerStingSSWT.IxController as XControllerStingSSWT; } }
    private XToolSswtSting xToolSswtSting { get{return((uControllerStingSSWT.ITool.IxTool) as XToolSswtSting);}}

    #endregion Constructors and Configuration

    #region private/protected Properties and Methods

    //The following are addresses in the PLC, NOT SPEED VALUES
    ushort StingModbusCmdForward1stStep = 4370;
    ushort StingModbusCmdForward2ndStep = 4626;
    ushort StingModbusCmdForward3rdStep = 4882;
    ushort StingModbusCmdForward4thStep = 5138;
    ushort StingModbusCmdForward5thStep = 5394;

    ushort StingModbusCmdReverse1stStep = 4386;
    ushort StingModbusCmdReverse2ndStep = 4642;
    ushort StingModbusCmdReverse3rdStep = 4898;
    ushort StingModbusCmdReverse4thStep = 5154;
    ushort StingModbusCmdReverse5thStep = 5410;

    private void InitHw()
    {
      if (IToolUSAFAControllerSting != null)
      {
        demoMode = false;
        demoMode = !(IToolUSAFAControllerSting.SSWT_Initialize_Sting_Mover());
      }
    }

    private bool alphaMovingForward = true;
    private bool betaMovingForward = true;
    /// <summary>
    /// this is used to throttle the intermediate update responses back to TvTng. the TCP, the Test Control Program
    /// </summary>
    private int updateCounter = 0;
    private object modbusLockObject = new object();

    protected override void timerServoCmndActive_Tick(object sender, EventArgs e)
    {
      if (demoMode)
      {
        base.timerServoCmndActive_Tick(sender, e);
        return;
      }
      int angleEncoderReading;
      ushort driveStepSpeedLevelCommand = 0;

      #region MovingAlpha
      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Alpha));
      currentValueAlpha = getAngleFromEncoderReading(StingChannel.Alpha, angleEncoderReading);

      float angleDeltaAbsoluteALPHA = 0;
      float angleDeltaAbsoluteBETA = 0;

      if (isMovingAlpha)
      {
        angleDeltaAbsoluteALPHA = Math.Abs(currentValueAlpha - targetAlpha);
        //check for move complete
        if (angleDeltaAbsoluteALPHA < xToolSswtSting.AlphaMoveCompleteTolerance)
        {
          isMovingAlpha = false;
          IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Alpha);
        }
        else
        {
            // check for next position move command
            if (currentValueAlpha < targetAlpha)
              alphaMovingForward = true;
            else
              alphaMovingForward = false;
            //IToolUSAFAControllerSting.Sting_Set_Polarity(alphaMovingForward, StingChannel.Alpha);

            if (angleDeltaAbsoluteALPHA < 1f)
              if (alphaMovingForward)
                driveStepSpeedLevelCommand = StingModbusCmdForward1stStep;
              else
                driveStepSpeedLevelCommand = StingModbusCmdReverse1stStep;
            else if (angleDeltaAbsoluteALPHA < 1.5f)
              if (alphaMovingForward)
                driveStepSpeedLevelCommand = StingModbusCmdForward2ndStep;
              else
                driveStepSpeedLevelCommand = StingModbusCmdReverse2ndStep;
            else if (angleDeltaAbsoluteALPHA < 2.25f)
              if (alphaMovingForward)
                driveStepSpeedLevelCommand = StingModbusCmdForward3rdStep;
              else
                driveStepSpeedLevelCommand = StingModbusCmdReverse3rdStep;
            else if (angleDeltaAbsoluteALPHA < 3.0f)
              if (alphaMovingForward)
                driveStepSpeedLevelCommand = StingModbusCmdForward4thStep;
              else
                driveStepSpeedLevelCommand = StingModbusCmdReverse4thStep;
            else
              if (alphaMovingForward)
                driveStepSpeedLevelCommand = StingModbusCmdForward5thStep;
              else
                driveStepSpeedLevelCommand = StingModbusCmdReverse5thStep;

            lock (modbusLockObject)
            {
              if (!stopNOW && isMovingAlpha)
                IToolUSAFAControllerSting.Sting_Set_Drive_Step_Level(StingChannel.Alpha, driveStepSpeedLevelCommand);
            }
        }
      }
#endregion MovingAlpha

      #region MovingBeta
      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Beta));
      currentValueBeta = getAngleFromEncoderReading(StingChannel.Beta, angleEncoderReading);

      if (isMovingBeta)
      {
        angleDeltaAbsoluteBETA = Math.Abs(currentValueBeta - targetBeta);
        //check for move complete
        if (angleDeltaAbsoluteBETA < xToolSswtSting.BetaMoveCompleteTolerance)
        {
          isMovingBeta = false;
          IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Beta);
        }
        else
        {
          // check for next position move command
          if (currentValueBeta < targetBeta)
            betaMovingForward = true;
          else
            betaMovingForward = false;
          //IToolUSAFAControllerSting.Sting_Set_Polarity(betaMovingForward, StingChannel.Beta);

          if (angleDeltaAbsoluteBETA < 1f)
            if (!betaMovingForward)
              driveStepSpeedLevelCommand = StingModbusCmdForward1stStep;
            else
              driveStepSpeedLevelCommand = StingModbusCmdReverse1stStep;
          else if (angleDeltaAbsoluteBETA < 1.5f)
            if (!betaMovingForward)
              driveStepSpeedLevelCommand = StingModbusCmdForward2ndStep;
            else
              driveStepSpeedLevelCommand = StingModbusCmdReverse2ndStep;
          else if (angleDeltaAbsoluteBETA < 2.25f)
            if (!betaMovingForward)
              driveStepSpeedLevelCommand = StingModbusCmdForward3rdStep;
            else
              driveStepSpeedLevelCommand = StingModbusCmdReverse3rdStep;
          else if (angleDeltaAbsoluteBETA < 3.0f)
            if (!betaMovingForward)
              driveStepSpeedLevelCommand = StingModbusCmdForward4thStep;
            else
              driveStepSpeedLevelCommand = StingModbusCmdReverse4thStep;
          else
            if (!betaMovingForward)
              driveStepSpeedLevelCommand = StingModbusCmdForward5thStep;
            else
              driveStepSpeedLevelCommand = StingModbusCmdReverse5thStep;

          lock (modbusLockObject)
          {
            if(!stopNOW && isMovingBeta)
              IToolUSAFAControllerSting.Sting_Set_Drive_Step_Level(StingChannel.Beta, driveStepSpeedLevelCommand);
          }
        }
      }
#endregion MovingBeta

      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Alpha));
      AngleAlpha = getAngleFromEncoderReading(StingChannel.Alpha, angleEncoderReading);
      angleDeltaAbsoluteALPHA = Math.Abs(AngleAlpha - targetAlpha);
      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Beta));
      AngleBeta = getAngleFromEncoderReading(StingChannel.Beta, angleEncoderReading);
      angleDeltaAbsoluteBETA = Math.Abs(AngleBeta - targetBeta);

      lock (modbusLockObject)
      {
        if (stopNOW)
        {
          stopNOW = false;
          isMovingAlpha = false;
          isMovingBeta = false;
          IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Alpha);
          IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Beta);
        }
        if (!isMovingAlpha && !isMovingBeta)
        {
          //moveComplete = true;
          this.timerServoCmndActive.Enabled = false;
          UControllerSting.ControllerMoveComplete();
        }
        else if (UControllerSting.CurrentControllerGotoValues.SendIntermediateReadingsWhileMoving &&
                  ((isMovingAlpha && (angleDeltaAbsoluteALPHA > 0.5)) || //DO NOT send intermediate values at the end of a move
                  (isMovingBeta && (angleDeltaAbsoluteBETA > 0.5))))    // it interferes with the move complete
        {
          if (updateCounter++ == 5)
          {
            updateCounter = 0;
            UControllerSting.CurrentControllerGotoValues.MoveComplete = false;
            ToolTcpSend(UControllerSting.CurrentControllerGotoValues); //send intermediate values back to TvTng
          }
        }
      }
    }

    private float getAngleFromEncoderReading(StingChannel stingChannel, int encoderReading)
    {
      float highAngleSetPoint;
      int highEncoderSetPoint;
      float encoderClicksPerDegree;
      if (stingChannel == StingChannel.Alpha)
      {
        highAngleSetPoint = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.AngleAlphaHigh;
        highEncoderSetPoint = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.EncoderReadingHighAlpha;
        encoderClicksPerDegree = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.EncoderClicksPerDegreeAlpha;
      }
      else
      {
        highAngleSetPoint = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.AngleBetaHigh;
        highEncoderSetPoint = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.EncoderReadingHighBeta;
        encoderClicksPerDegree = uControllerStingSSWT.XControllerStingSSWT_CALIBRATION.EncoderClicksPerDegreeBeta;
      }
      return highAngleSetPoint - ((highEncoderSetPoint - encoderReading)/encoderClicksPerDegree);
    }

    #endregion private/protected Properties and Methods

    #region Public Properties and Methods

    public bool UpdateCurrentAlphaBetaValues()
    {
      if (demoMode)
      {
        return true;
      }

      try
      {
        int angleEncoderReading;
        angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Alpha));
        AngleAlpha = getAngleFromEncoderReading(StingChannel.Alpha, angleEncoderReading);
        angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Beta));
        AngleBeta = getAngleFromEncoderReading(StingChannel.Beta, angleEncoderReading);

        // THE FOLLOWING really messed up the SYNC with TvTng
        //if (UControllerSting.CurrentControllerGotoValues == null)
        //  return false;
        //UControllerSting.CurrentControllerGotoValues.ControllerName = uControllerStingSSWT.ControllerName;
        //UControllerSting.CurrentControllerGotoValues.ControllerID = uControllerStingSSWT.ControllerID;
        //UControllerSting.CurrentControllerGotoValues.TcpExpectedEventCompleteType = Core.TestExpectedEventCompleteType.None;
        //UControllerSting.CurrentControllerGotoValues.MoveComplete = false;
        //ToolTcpSend(UControllerSting.CurrentControllerGotoValues); //send intermediate values back to TvTng
      }
      catch (Exception)
      {
        return false;
      }
      return true;
    }

    #endregion Public Properties and Methods

    #region IController Members

    public override void GotoPosition(ControllerGotoValues cgv)
    {
      if (demoMode)
      {
        base.GotoPosition(cgv);
        return;
      }

      int angleEncoderReading;
      float angleDeltaAbsoluteALPHA = 0;
      float angleDeltaAbsoluteBETA = 0;

      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Alpha));
      AngleAlpha = getAngleFromEncoderReading(StingChannel.Alpha, angleEncoderReading);
      targetAlpha = Angle.Convert(cgv.ValuesArray[0], UControllerStingAB.XControllerSting.AngleUnit, AngleUnits.degrees);
      angleDeltaAbsoluteALPHA = Math.Abs(AngleAlpha - targetAlpha);
      //check for move complete
      if (angleDeltaAbsoluteALPHA > xToolSswtSting.AlphaMoveCompleteTolerance)
      {
        isMovingAlpha = true;
      }

      angleEncoderReading = (int)(IToolUSAFAControllerSting.Sting_Read_Position_Voltage(StingChannelTypes.Beta));
      AngleBeta = getAngleFromEncoderReading(StingChannel.Beta, angleEncoderReading);
      targetBeta = Angle.Convert(cgv.ValuesArray[1], UControllerStingAB.XControllerSting.AngleUnit, AngleUnits.degrees);
      angleDeltaAbsoluteBETA = Math.Abs(AngleBeta - targetBeta);
      //check for move complete
      if (angleDeltaAbsoluteBETA > xToolSswtSting.BetaMoveCompleteTolerance)
      {
        isMovingBeta = true;
      }

      this.timerServoCmndActive.Enabled = true;
      
    }

    bool stopNOW = false;

    public override void stopRunning()
    {
      if (demoMode)
      {
        base.stopRunning();
        return;
      }

      lock (modbusLockObject)
      {
        if(isMovingAlpha || isMovingBeta)
          stopNOW = true;
        // this is called asynchronously, so protect against race conditions.
        isMovingAlpha = false;
        isMovingBeta = false;

        IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Alpha);
        IToolUSAFAControllerSting.Sting_Apply_Brake(true, StingChannel.Beta);
      }
    }

    #endregion IController Members

    #region IControllerStingHW

    //public override void setCalibration(StingCalibrate stingCalibrate)
    //{
    //  //DO NOT do anything. This was called from the FTool side.
    //}

    public override void stingGetValue(StingGetValue stingGetValue)
    {
      stingGetValue.Value = IToolUSAFAControllerSting.Sting_Read_Position_Voltage(stingGetValue.StingChannelType);
      uControllerStingSSWT.stingGetValueReturn(stingGetValue); //update the FTool version UControllerSting
      ToolTcpSend(stingGetValue); //Update the UTool version UControllerSTing
    }

    #endregion IControllerStingHW
  } //End of class HwControllerStingSSWT
}
