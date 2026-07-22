using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows.Forms;

namespace Tool_LSWT_Sting
{
  static class Program
  {
    /// <summary>
    /// The main entry point for the application.
    /// </summary>
    [STAThread]
    static void Main(string[] args)
    {
      Application.EnableVisualStyles();
      Application.SetCompatibleTextRenderingDefault(false);
      if ((args != null) && (args.Length > 0))
        Application.Run(new FTool_LSWT_Sting(args));
      else
        Application.Run(new FTool_LSWT_Sting());
    }
  }
}
