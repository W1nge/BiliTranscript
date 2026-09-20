using System.Numerics;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Graphics.Canvas.Effects;
using Windows.UI;
using Windows.UI.Composition;
using Windows.UI.Composition.Desktop;

namespace BiliTranscript.Backdrop;

internal static unsafe class BackdropExports
{
    private const uint WS_POPUP = 0x80000000;
    private const uint WS_EX_TRANSPARENT = 0x00000020;
    private const uint WS_EX_TOOLWINDOW = 0x00000080;
    private const uint WS_EX_NOACTIVATE = 0x08000000;
    private const uint WS_EX_NOREDIRECTIONBITMAP = 0x00200000;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_NOOWNERZORDER = 0x0200;
    private const uint SWP_NOZORDER = 0x0004;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOSIZE = 0x0001;
    private const int SW_SHOWNA = 8;
    private const int SW_HIDE = 0;
    private const uint WM_NCHITTEST = 0x0084;
    private const uint WM_MOUSEACTIVATE = 0x0021;
    private const nint HTTRANSPARENT = -1;
    private const nint MA_NOACTIVATE = 3;

    private static readonly object Gate = new();
    private static readonly Dictionary<nint, BackdropInstance> Instances = new();
    private static readonly WndProc WindowProcedure = BackdropWindowProc;
    private static readonly string WindowClassName = $"BiliTranscriptBackdrop_{Environment.ProcessId}";
    private static bool _windowClassRegistered;
    private static nint _win2DActivationContext;
    private static string _lastError = "";

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_create", CallConvs = [typeof(CallConvCdecl)])]
    public static nint Create(nint hostHwnd, uint tintArgb, float blurAmount, float saturation)
    {
        try
        {
            EnsureWindowClass();
            var instance = new BackdropInstance(hostHwnd, tintArgb, blurAmount, saturation);
            var handle = GCHandle.Alloc(instance);
            var token = GCHandle.ToIntPtr(handle);
            lock (Gate)
            {
                Instances[token] = instance;
            }
            return token;
        }
        catch (Exception ex)
        {
            _lastError = ex.ToString();
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_last_error", CallConvs = [typeof(CallConvCdecl)])]
    public static int LastError(byte* buffer, int capacity)
    {
        if (buffer == null || capacity <= 0) return 0;
        var bytes = Encoding.UTF8.GetBytes(_lastError);
        var length = Math.Min(bytes.Length, capacity - 1);
        for (var index = 0; index < length; index++) buffer[index] = bytes[index];
        buffer[length] = 0;
        return length;
    }

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_sync", CallConvs = [typeof(CallConvCdecl)])]
    public static int Sync(nint token, int x, int y, int width, int height, int syncZOrder)
    {
        try
        {
            if (!TryGet(token, out var instance)) return 0;
            instance.Sync(x, y, width, height, syncZOrder != 0);
            return 1;
        }
        catch
        {
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_show", CallConvs = [typeof(CallConvCdecl)])]
    public static int Show(nint token, int visible)
    {
        try
        {
            if (!TryGet(token, out var instance)) return 0;
            ShowWindow(instance.Hwnd, visible != 0 ? SW_SHOWNA : SW_HIDE);
            return 1;
        }
        catch
        {
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_update", CallConvs = [typeof(CallConvCdecl)])]
    public static int Update(nint token, uint tintArgb, float blurAmount, float saturation)
    {
        try
        {
            if (!TryGet(token, out var instance)) return 0;
            instance.Update(tintArgb, blurAmount, saturation);
            return 1;
        }
        catch
        {
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "bt_backdrop_destroy", CallConvs = [typeof(CallConvCdecl)])]
    public static void Destroy(nint token)
    {
        BackdropInstance? instance;
        lock (Gate)
        {
            if (!Instances.Remove(token, out instance)) return;
        }
        instance.Dispose();
        GCHandle.FromIntPtr(token).Free();
    }

    private static bool TryGet(nint token, out BackdropInstance instance)
    {
        lock (Gate)
        {
            return Instances.TryGetValue(token, out instance!);
        }
    }

    private static void EnsureWindowClass()
    {
        if (_windowClassRegistered) return;
        var module = GetModuleHandleW(null);
        var windowClass = new WNDCLASSEXW
        {
            cbSize = (uint)sizeof(WNDCLASSEXW),
            hInstance = module,
            lpfnWndProc = Marshal.GetFunctionPointerForDelegate(WindowProcedure),
            lpszClassName = Marshal.StringToHGlobalUni(WindowClassName),
        };
        try
        {
            if (RegisterClassExW(&windowClass) == 0)
                throw new InvalidOperationException("RegisterClassExW failed");
            _windowClassRegistered = true;
        }
        finally
        {
            Marshal.FreeHGlobal(windowClass.lpszClassName);
        }
    }

    private static nuint ActivateWin2DContext()
    {
        lock (Gate)
        {
            if (_win2DActivationContext == 0)
            {
                var module = GetModuleHandleW("BiliTranscript.Backdrop.dll");
                if (module == 0) throw new InvalidOperationException("Backdrop module handle is unavailable");
                var modulePath = new StringBuilder(32768);
                if (GetModuleFileNameW(module, modulePath, modulePath.Capacity) == 0)
                    throw new InvalidOperationException("Backdrop module path is unavailable");
                var directory = Path.GetDirectoryName(modulePath.ToString())
                    ?? throw new InvalidOperationException("Backdrop runtime directory is unavailable");

                // Reg-free WinRT is the supported way for an unpackaged desktop
                // process to activate Win2D runtime classes.  The context is
                // process-local and does not modify the registry.
                var activationContext = new ACTCTXW
                {
                    cbSize = (uint)Marshal.SizeOf<ACTCTXW>(),
                    lpSource = Path.Combine(directory, "BiliTranscript.Backdrop.manifest"),
                };
                _win2DActivationContext = CreateActCtxW(ref activationContext);
                if (_win2DActivationContext == -1)
                {
                    _win2DActivationContext = 0;
                    throw new InvalidOperationException($"CreateActCtxW failed ({Marshal.GetLastWin32Error()})");
                }
            }

            // Win2D's UWP binary imports the APP CRT forwarders.  Load them
            // explicitly so unpackaged desktop processes resolve them from
            // the bridge directory rather than relying on package identity.
            // Python preloads these files by absolute path; these calls retain
            // the ordinary packaged-app behavior when the bridge is reused.
            LoadLibraryW("msvcp140_app.dll");
            LoadLibraryW("vcruntime140_1_app.dll");
            LoadLibraryW("vcruntime140_app.dll");
            if (!ActivateActCtx(_win2DActivationContext, out var cookie))
                throw new InvalidOperationException($"ActivateActCtx failed ({Marshal.GetLastWin32Error()})");
            return cookie;
        }
    }

    private static nint BackdropWindowProc(nint hwnd, uint message, nuint wParam, nint lParam)
    {
        // WS_EX_TRANSPARENT changes paint ordering; it does not guarantee that
        // a top-level window is absent from mouse hit-testing.  Returning
        // HTTRANSPARENT is what makes this visual-only HWND truly click-through.
        if (message == WM_NCHITTEST) return HTTRANSPARENT;
        if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
        return DefWindowProcW(hwnd, message, wParam, lParam);
    }

    private sealed class BackdropInstance : IDisposable
    {
        private readonly nint _hostHwnd;
        private readonly nint _dispatcherQueueController;
        private readonly Compositor _compositor;
        private readonly DesktopWindowTarget _target;
        private readonly CompositionEffectBrush _blurBrush;
        private readonly CompositionColorBrush _tintBrush;
        public nint Hwnd { get; }

        public BackdropInstance(nint hostHwnd, uint tintArgb, float blurAmount, float saturation)
        {
            var initializeResult = RoInitialize(0);
            if (initializeResult < 0 && initializeResult != unchecked((int)0x80010106))
                Marshal.ThrowExceptionForHR(initializeResult);
            var queueOptions = new DispatcherQueueOptions
            {
                dwSize = (uint)sizeof(DispatcherQueueOptions),
                threadType = 2,
                apartmentType = 2,
            };
            var queueResult = CreateDispatcherQueueController(queueOptions, out _dispatcherQueueController);
            if (queueResult < 0)
                Marshal.ThrowExceptionForHR(queueResult);
            var activationCookie = ActivateWin2DContext();
            _hostHwnd = hostHwnd;
            try
            {
                Hwnd = CreateWindowExW(
                    WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_NOREDIRECTIONBITMAP,
                    WindowClassName,
                    "",
                    WS_POPUP,
                    0,
                    0,
                    1,
                    1,
                    0,
                    0,
                    GetModuleHandleW(null),
                    0);
                if (Hwnd == 0) throw new InvalidOperationException("CreateWindowExW failed");
                EnableHostBackdrop(Hwnd);

                _compositor = new Compositor();
                _target = CreateDesktopWindowTarget(_compositor, Hwnd);
                var root = _compositor.CreateContainerVisual();
                root.RelativeSizeAdjustment = Vector2.One;
                _target.Root = root;

                var backdrop = _compositor.CreateHostBackdropBrush();
                var blur = new GaussianBlurEffect
                {
                    Name = "BackdropBlur",
                    BlurAmount = blurAmount,
                    BorderMode = EffectBorderMode.Hard,
                    Optimization = EffectOptimization.Speed,
                    Source = new CompositionEffectSourceParameter("Backdrop"),
                };
                var effect = new SaturationEffect
                {
                    Name = "BackdropSaturation",
                    Saturation = saturation,
                    Source = blur,
                };
                // Blur and saturation are intentionally fixed for the lifetime
                // of a backdrop.  Avoiding the animatable-property collection
                // also avoids a managed CCW on this NativeAOT boundary.
                var factory = _compositor.CreateEffectFactory(effect);
                _blurBrush = factory.CreateBrush();
                _blurBrush.SetSourceParameter("Backdrop", backdrop);
                var backdropVisual = _compositor.CreateSpriteVisual();
                backdropVisual.RelativeSizeAdjustment = Vector2.One;
                backdropVisual.Brush = _blurBrush;
                root.Children.InsertAtBottom(backdropVisual);

                _tintBrush = _compositor.CreateColorBrush(ToColor(tintArgb));
                var tintVisual = _compositor.CreateSpriteVisual();
                tintVisual.RelativeSizeAdjustment = Vector2.One;
                tintVisual.Brush = _tintBrush;
                root.Children.InsertAtTop(tintVisual);
            }
            finally
            {
                DeactivateActCtx(0, activationCookie);
            }
        }

        public void Sync(int x, int y, int width, int height, bool syncZOrder)
        {
            // Position without changing either top-level window's Z order.
            SetWindowPos(
                Hwnd,
                0,
                x,
                y,
                Math.Max(1, width),
                Math.Max(1, height),
                SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOZORDER);
            if (syncZOrder)
            {
                // ShowWindow can raise a popup.  Only show from this final
                // synchronization path, then immediately put the backdrop
                // below the Qt host.  Live move/resize callbacks must never
                // call ShowWindow or they will temporarily cover the UI.
                ShowWindow(Hwnd, SW_SHOWNA);
                // Insert the visual-only window immediately after (below) the
                // Qt host in the desktop Z order.
                SetWindowPos(
                    Hwnd,
                    _hostHwnd,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOMOVE | SWP_NOSIZE);
            }
        }

        public void Update(uint tintArgb, float blurAmount, float saturation)
        {
            _tintBrush.Color = ToColor(tintArgb);
        }

        public void Dispose()
        {
            ShowWindow(Hwnd, SW_HIDE);
            _target.Dispose();
            _blurBrush.Dispose();
            _tintBrush.Dispose();
            _compositor.Dispose();
            if (_dispatcherQueueController != 0) Marshal.Release(_dispatcherQueueController);
            DestroyWindow(Hwnd);
        }
    }

    private static DesktopWindowTarget CreateDesktopWindowTarget(Compositor compositor, nint hwnd)
    {
        var unknown = ((WinRT.IWinRTObject)compositor).NativeObject.ThisPtr;
        var iid = new Guid("29E691FA-4567-4DCA-B319-D0F207EB6807");
        Marshal.ThrowExceptionForHR(Marshal.QueryInterface(unknown, ref iid, out var interopPointer));
        try
        {
            nint targetPointer = 0;
            var vtable = *(nint**)interopPointer;
            var createTarget = (delegate* unmanaged[Stdcall]<nint, nint, int, nint*, int>)vtable[3];
            Marshal.ThrowExceptionForHR(createTarget(interopPointer, hwnd, 0, &targetPointer));
            try
            {
                return WinRT.MarshalInspectable<DesktopWindowTarget>.FromAbi(targetPointer);
            }
            finally { Marshal.Release(targetPointer); }
        }
        finally { Marshal.Release(interopPointer); }
    }

    private static Color ToColor(uint argb) => Color.FromArgb(
        (byte)(argb >> 24),
        (byte)(argb >> 16),
        (byte)(argb >> 8),
        (byte)argb);

    private static void EnableHostBackdrop(nint hwnd)
    {
        // Windows 11 exposes the documented DWM switch.  Windows 10 requires
        // the host-backdrop accent state to make CreateHostBackdropBrush sample
        // outside an unpackaged Win32 window.  State 5 only enables the source;
        // the actual blur remains the retained Composition effect graph.
        var enabled = 1;
        if (DwmSetWindowAttribute(hwnd, 38, &enabled, sizeof(int)) == 0) return;

        var policy = new ACCENT_POLICY
        {
            AccentState = 5, // ACCENT_ENABLE_HOSTBACKDROP
            AccentFlags = 0,
            GradientColor = 0,
            AnimationId = 0,
        };
        var data = new WINDOWCOMPOSITIONATTRIBDATA
        {
            Attribute = 19, // WCA_ACCENT_POLICY
            Data = &policy,
            SizeOfData = (nuint)sizeof(ACCENT_POLICY),
        };
        SetWindowCompositionAttribute(hwnd, &data);
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WNDCLASSEXW
    {
        public uint cbSize;
        public uint style;
        public nint lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public nint hInstance;
        public nint hIcon;
        public nint hCursor;
        public nint hbrBackground;
        public nint lpszMenuName;
        public nint lpszClassName;
        public nint hIconSm;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DispatcherQueueOptions
    {
        public uint dwSize;
        public int threadType;
        public int apartmentType;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ACCENT_POLICY
    {
        public int AccentState;
        public int AccentFlags;
        public uint GradientColor;
        public int AnimationId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WINDOWCOMPOSITIONATTRIBDATA
    {
        public int Attribute;
        public void* Data;
        public nuint SizeOfData;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ACTCTXW
    {
        public uint cbSize;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpSource;
        public ushort wProcessorArchitecture;
        public ushort wLangId;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpAssemblyDirectory;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpResourceName;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpApplicationName;
        public nint hModule;
    }

    [UnmanagedFunctionPointer(CallingConvention.Winapi)]
    private delegate nint WndProc(nint hwnd, uint message, nuint wParam, nint lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassExW(WNDCLASSEXW* windowClass);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint CreateWindowExW(uint exStyle, string className, string windowName, uint style, int x, int y, int width, int height, nint parent, nint menu, nint instance, nint param);

    [DllImport("user32.dll")]
    private static extern nint DefWindowProcW(nint hwnd, uint message, nuint wParam, nint lParam);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(nint hwnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(nint hwnd, int command);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(nint hwnd, nint insertAfter, int x, int y, int width, int height, uint flags);

    [DllImport("user32.dll")]
    private static extern bool SetWindowCompositionAttribute(nint hwnd, WINDOWCOMPOSITIONATTRIBDATA* data);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, void* value, int valueSize);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern nint GetModuleHandleW(string? moduleName);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetModuleFileNameW(nint module, StringBuilder fileName, int size);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint LoadLibraryW(string fileName);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern nint CreateActCtxW(ref ACTCTXW activationContext);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ActivateActCtx(nint activationContext, out nuint cookie);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DeactivateActCtx(uint flags, nuint cookie);

    [DllImport("combase.dll")]
    private static extern int RoInitialize(uint initializationType);

    [DllImport("CoreMessaging.dll")]
    private static extern int CreateDispatcherQueueController(DispatcherQueueOptions options, out nint dispatcherQueueController);
}
