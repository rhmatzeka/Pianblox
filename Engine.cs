using System;
using System.Diagnostics;
using System.Runtime;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// The playback loop lives here rather than in PowerShell: the interpreter
// allocates on every note, and the resulting GC pauses are far larger than the
// timing tolerance we need. This loop allocates nothing once it starts.
//
// Because the whole performance is one .NET call, PowerShell never gets a turn
// to notice Ctrl+C, so the loop watches for it - and for its own hotkeys -
// itself, polling inside the waits rather than only between notes.
public static class PianoEngine
{
    [DllImport("user32.dll")] static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    [DllImport("user32.dll")] static extern uint MapVirtualKey(uint uCode, uint uMapType);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("winmm.dll")] static extern uint timeBeginPeriod(uint p);
    [DllImport("winmm.dll")] static extern uint timeEndPeriod(uint p);

    const uint KEYUP    = 2;
    const byte VK_SHIFT = 0x10;
    const int  VK_END   = 0x23;   // stop
    const int  VK_F8    = 0x77;   // pause / resume

    const int GO = 0, STOP = 1, REBASE = 2;

    public static double[] Err;        // actual minus scheduled, per instant
    public static int      Played;
    public static bool     Aborted;
    public static double   PausedMs;
    public static double   KeyPausedMs;    // held by F8
    public static double   FocusPausedMs;  // held because Roblox was not in front
    public static double   TotalMs;

    static readonly StringBuilder Buf  = new StringBuilder(512);
    static readonly byte[]        Scan = new byte[256];
    static readonly char[]        Want = "roblox".ToCharArray();
    static readonly double        TicksPerMs = Stopwatch.Frequency / 1000.0;
    static readonly bool[]        DownW = new bool[256];   // held as white keys
    static readonly bool[]        DownB = new bool[256];   // held as black keys

    static bool   _noInput, _guard, _stop, _paused, _f8Was, _shiftDown;
    static double _rebase;

    // Scanning the buffer in place keeps the focus check allocation-free.
    static bool Focused()
    {
        if (!_guard) return true;
        Buf.Length = 0;
        GetWindowText(GetForegroundWindow(), Buf, Buf.Capacity);
        int n = Buf.Length, m = Want.Length;
        for (int i = 0; i + m <= n; i++)
        {
            int j = 0;
            while (j < m && char.ToLowerInvariant(Buf[i + j]) == Want[j]) j++;
            if (j == m) return true;
        }
        return false;
    }

    static void Key(byte vk, bool down)
    {
        if (_noInput) return;
        keybd_event(vk, Scan[vk], down ? 0u : KEYUP, UIntPtr.Zero);
    }

    // Nothing may be left sounding while we are stopped or held.
    static void LiftAll()
    {
        bool anyBlack = false;
        for (int k = 0; k < 256; k++) if (DownB[k]) { anyBlack = true; break; }
        if (anyBlack)
        {
            if (!_shiftDown) { Key(VK_SHIFT, true); _shiftDown = true; }
            for (int k = 0; k < 256; k++) if (DownB[k]) { Key((byte)k, false); DownB[k] = false; }
        }
        if (_shiftDown) { Key(VK_SHIFT, false); _shiftDown = false; }
        for (int k = 0; k < 256; k++) if (DownW[k]) { Key((byte)k, false); DownW[k] = false; }
    }

    public static void Focus(IntPtr hWnd) { SetForegroundWindow(hWnd); }

    // Presses END for real, so another running player stops through its own
    // clean shutdown and releases every key it was holding.
    public static void TapEnd()
    {
        byte vk = (byte)VK_END;
        byte sc = (byte)MapVirtualKey(vk, 0);
        keybd_event(vk, sc, 0, UIntPtr.Zero);
        Thread.Sleep(150);
        keybd_event(vk, sc, KEYUP, UIntPtr.Zero);
    }

    static bool EndPressed() { return (GetAsyncKeyState(VK_END) & 0x8000) != 0; }

    static bool F8Edge()
    {
        bool now = (GetAsyncKeyState(VK_F8) & 0x8000) != 0;
        bool hit = now && !_f8Was;
        _f8Was = now;
        return hit;
    }

    // Called from inside the waits, so stopping and pausing feel immediate even
    // when the next note is seconds away.
    static int Poll(Stopwatch sw)
    {
        if (_stop || EndPressed()) { _stop = true; return STOP; }
        if (F8Edge()) { _paused = !_paused; if (_paused) Console.WriteLine("  [paused - F8 to resume, END to stop]"); }

        bool lost = !Focused();
        if (!_paused && !lost) return GO;

        bool manual = _paused;                      // why we are holding
        if (lost && !manual) Console.WriteLine("  [paused - Roblox lost focus]");
        LiftAll();                                  // never drone while held

        double t0 = sw.ElapsedTicks / TicksPerMs;
        while (true)
        {
            Thread.Sleep(30);
            if (_stop || EndPressed()) { _stop = true; return STOP; }
            if (F8Edge()) _paused = !_paused;
            if (!_paused && Focused()) break;
        }
        Console.WriteLine("  [resumed]");

        _rebase = sw.ElapsedTicks / TicksPerMs - t0;
        PausedMs += _rebase;
        if (manual) KeyPausedMs += _rebase; else FocusPausedMs += _rebase;
        return REBASE;
    }

    // Reading ElapsedTicks is far cheaper than Elapsed.TotalMilliseconds, which
    // builds a TimeSpan every call - and this runs thousands of times per note.
    // Sleep can overshoot by a whole timer tick even at 1 ms resolution, so the
    // last stretch is spun out instead, and the sleeps before it are chopped
    // short so the hotkeys stay responsive.
    static int WaitUntil(Stopwatch sw, double ms)
    {
        long target = (long)(ms * TicksPerMs);
        while (true)
        {
            long r = target - sw.ElapsedTicks;
            if (r <= 0) return GO;
            double left = r / TicksPerMs;
            if (left > 16.0)
            {
                int st = Poll(sw);
                if (st != GO) return st;
                Thread.Sleep((int)Math.Min(left - 15.0, 20.0));
            }
            else Thread.SpinWait(30);
        }
    }

    // The schedule is a list of instants. At each one some keys are lifted and
    // others struck, so notes may overlap and hold for their own lengths
    // instead of every note owning an equal slice of a grid.
    //
    // White and black keys are struck apart: Shift must be down for a black key
    // and up for a white one, so a chord holding both cannot share one Shift
    // state. Whites go down first, then Shift plus the blacks.
    public static void Play(double[] pressAt, byte[][] relWhite, byte[][] relBlack,
                            byte[][] white, byte[][] black,
                            bool noInput, bool guard, int progressEvery)
    {
        for (int i = 0; i < 256; i++) Scan[i] = (byte)MapVirtualKey((uint)i, 0);
        for (int i = 0; i < 256; i++) { DownW[i] = false; DownB[i] = false; }

        Err = new double[pressAt.Length];
        Played = 0; Aborted = false; PausedMs = 0; KeyPausedMs = 0; FocusPausedMs = 0;
        _noInput = noInput; _guard = guard;
        _stop = false; _paused = false; _f8Was = false; _shiftDown = false;

        ConsoleCancelEventHandler onBreak = delegate(object s, ConsoleCancelEventArgs e)
        {
            e.Cancel = true;      // don't kill the host; unwind cleanly instead
            _stop = true;
        };
        Console.CancelKeyPress += onBreak;

        var oldMode = GCSettings.LatencyMode;
        var oldPrio = Thread.CurrentThread.Priority;
        var proc = Process.GetCurrentProcess();
        var oldClass = proc.PriorityClass;

        timeBeginPeriod(1);
        GCSettings.LatencyMode = GCLatencyMode.SustainedLowLatency;
        Thread.CurrentThread.Priority = ThreadPriority.Highest;
        try { proc.PriorityClass = ProcessPriorityClass.High; } catch { }
        GC.Collect();
        GC.WaitForPendingFinalizers();

        var sw = Stopwatch.StartNew();
        double off = 0;   // accumulated pause time; the schedule slides, tempo does not

        try
        {
            for (int i = 0; i < pressAt.Length; i++)
            {
                double at;
                bool stopped = false;
                while (true)
                {
                    at = pressAt[i] + off;
                    int st = WaitUntil(sw, at);
                    if (st == STOP)   { stopped = true; break; }
                    if (st == REBASE) { off += _rebase; continue; }
                    break;
                }
                if (stopped) { Aborted = true; break; }

                byte[] rw = relWhite[i];
                byte[] rb = relBlack[i];
                byte[] w  = white[i];
                byte[] b  = black[i];

                // Shift is held, not pulsed: a press-and-release measured in
                // microseconds can be gone by the time the game reads the
                // modifier. It must also match the key's colour when a key is
                // LIFTED, or the game works out the wrong note to silence and
                // the old one hangs, deaf to the next strike.
                //
                // White work first, so Shift is left down after any black
                // strike and stays there until a white key needs it up.
                if (rw.Length > 0 || w.Length > 0)
                {
                    if (_shiftDown) { Key(VK_SHIFT, false); _shiftDown = false; }
                    for (int j = 0; j < rw.Length; j++) { Key(rw[j], false); DownW[rw[j]] = false; }
                    for (int j = 0; j < w.Length;  j++) { Key(w[j],  true);  DownW[w[j]]  = true;  }
                }
                if (rb.Length > 0 || b.Length > 0)
                {
                    if (!_shiftDown) { Key(VK_SHIFT, true); _shiftDown = true; }
                    for (int j = 0; j < rb.Length; j++) { Key(rb[j], false); DownB[rb[j]] = false; }
                    for (int j = 0; j < b.Length;  j++) { Key(b[j],  true);  DownB[b[j]]  = true;  }
                }
                Err[i] = sw.ElapsedTicks / TicksPerMs - at;

                Played = i + 1;
                if (progressEvery > 0 && Played % progressEvery == 0)
                    Console.WriteLine("  " + Played + "/" + pressAt.Length + " instants  ("
                                      + ((int)(sw.Elapsed.TotalSeconds / 60)) + ":"
                                      + ((int)(sw.Elapsed.TotalSeconds % 60)).ToString("00") + ")");
            }
        }
        finally
        {
            LiftAll();
            TotalMs = sw.ElapsedTicks / TicksPerMs;
            Console.CancelKeyPress -= onBreak;
            try { proc.PriorityClass = oldClass; } catch { }
            Thread.CurrentThread.Priority = oldPrio;
            GCSettings.LatencyMode = oldMode;
            timeEndPeriod(1);
        }
    }
}
