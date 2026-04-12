#! python3
# -*- coding: utf-8 -*-
"""
Gemini MCP Server - Start Button Script

Uses direct drain_queue() on the script thread + Application.DoEvents()
to pump Windows messages, preventing the STA thread deadlock that occurs
when Revit's view regeneration fires COM callbacks after Transaction.Commit().
"""
import sys
import os
import time

# Robust path discovery
try:
    _cur_dir = os.path.dirname(os.path.abspath(__file__))
    # script.py is in .extension/.tab/.panel/.pushbutton/script.py
    # We need .extension/ level
    ext_root = os.path.dirname(os.path.dirname(os.path.dirname(_cur_dir)))
    lib_path = os.path.join(ext_root, 'lib')
    
    if ext_root not in sys.path:
        sys.path.append(ext_root)
    if lib_path not in sys.path:
        sys.path.append(lib_path)
except Exception as e:
    print("Path initialization failed: " + str(e))

# Hard reset of revit_mcp modules to force reload from disk
for m in list(sys.modules.keys()):
    if 'revit_mcp' in m:
        del sys.modules[m]

from pyrevit import revit, DB, UI, HOST_APP, forms, script
from Autodesk.Revit.UI import TaskDialog

# Track initialization status
_init_success = False
try:
    from revit_mcp.gemini_client import client
    from revit_mcp.dispatcher import orchestrator
    from revit_mcp import bridge
    client.log("UI: Hard module reset completed. v9-STABLE starting.")
    _init_success = True
except Exception as e:
    import traceback
    _init_error = "Pre-load failed: {}\n\nTraceback:\n{}".format(str(e), traceback.format_exc())
    print(_init_error)
    
    # Dummy client to prevent NameError crashes later
    class DummyClient:
        def log(self, *args, **kwargs): pass
    client = DummyClient()

import clr

# Import message pump - critical for STA thread compatibility
try:
    clr.AddReference('System.Windows.Forms')
    from System.Windows.Forms import Application as WinForms
    _has_doevents = True
except:
    _has_doevents = False

# Set up references for WPF
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xaml")
import System.Windows.Input 
import threading
from System.Windows import Window, HorizontalAlignment, Thickness, CornerRadius, TextWrapping
from System.Windows.Media import Brushes, Color, SolidColorBrush
from System.Windows.Controls import Border, TextBox
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Threading import DispatcherTimer
from System.Windows.Markup import XamlReader
from System.IO import FileStream, FileMode, FileAccess, FileShare
from System import TimeSpan, Action

# Add reference for keyboard interop
clr.AddReference("WindowsFormsIntegration")
from System.Windows.Forms.Integration import ElementHost

# Persistent reference to prevent garbage collection of the chat window
_current_chat_window = None

class AIChatWindow(object):
    """AI chat window for Gemini MCP (CPython/XamlReader compatible)."""
    def __init__(self, xaml_file):
        # Load the XAML root object
        stream = FileStream(xaml_file, FileMode.Open, FileAccess.Read, FileShare.Read)
        try:
            self.window = XamlReader.Load(stream)
        finally:
            stream.Close()
        
        self.history = []
        self.is_thinking = False
        self.cancelled = False
        self._uiapp = None
        self.setup_ui()

    def setup_ui(self):
        """Set up initial UI state."""
        # Find elements by name from the loaded window
        self.UserInput = self.window.FindName("UserInput")
        self.SendButton = self.window.FindName("SendButton")
        self.ChatHistory = self.window.FindName("ChatHistory")
        self.ChatScroller = self.window.FindName("ChatScroller")
        
        self.StopButton = self.window.FindName("StopButton")
        
        if self.UserInput:
            self.UserInput.Focus()
            self.UserInput.KeyDown += self.on_key_down
        if self.SendButton:
            self.SendButton.Click += self.on_send_click
        if self.StopButton:
            self.StopButton.Click += self.on_stop_click

    def add_message(self, message, is_user=True):
        """Add a message to the chat history block."""
        new_border = Border()
        new_border.CornerRadius = CornerRadius(8)
        new_border.Padding = Thickness(12)
        new_border.Margin = Thickness(0, 5, 0, 5)
        new_border.MaxWidth = 300
        
        if is_user:
            new_border.Background = SolidColorBrush(Color.FromRgb(0, 122, 204)) # Revit Blue
            new_border.HorizontalAlignment = HorizontalAlignment.Right
            new_border.Margin = Thickness(50, 5, 0, 5)
        else:
            new_border.Background = SolidColorBrush(Color.FromRgb(45, 45, 45)) # Dark Grey
            new_border.HorizontalAlignment = HorizontalAlignment.Left
            new_border.Margin = Thickness(0, 5, 50, 5)

        txt = TextBox()
        txt.Text = message
        txt.TextWrapping = TextWrapping.Wrap
        txt.Foreground = Brushes.White
        txt.IsReadOnly = True
        txt.BorderThickness = Thickness(0)
        txt.Background = Brushes.Transparent
        # Allow selection while keeping it looking like a label
        txt.VerticalScrollBarVisibility = System.Windows.Controls.ScrollBarVisibility.Disabled
        
        new_border.Child = txt
        if self.ChatHistory:
            self.ChatHistory.Children.Add(new_border)
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()
        
        # Add to history
        self.history.append({"text": message, "is_user": is_user})

    def on_send_click(self, sender, e):
        """Handle the send button click."""
        try:
            if not self.UserInput: return
            user_text = self.UserInput.Text.strip()
            if user_text:
                if user_text.lower() == "ping":
                    self.UserInput.Text = ""
                    self.add_message("ping", is_user=True)
                    self.test_bridge()
                    return
                
                self.UserInput.Text = ""
                self.add_message(user_text, is_user=True)
                
                # Show thinking state
                self.is_thinking = True
                self.cancelled = False
                if self.StopButton:
                    import System.Windows
                    self.StopButton.Visibility = System.Windows.Visibility.Visible
                if self.SendButton:
                    self.SendButton.IsEnabled = False

                thinking_msg = "Thinking..."
                self.add_message(thinking_msg, is_user=False)
                
                # Run Gemini call in separate thread
                client.log("UI Thread: Dispatching prompt: " + user_text[:30])
                thread = threading.Thread(target=self.get_gemini_response, args=(user_text,))
                thread.daemon = True
                thread.start()
                client.log("UI Thread: Thread.start() called.")
        except Exception as ex:
            import traceback
            from pyrevit import forms
            forms.alert("UI Error: {}\n\n{}".format(str(ex), traceback.format_exc()))

    def on_stop_click(self, sender, e):
        """Handle the stop button click."""
        self.cancelled = True
        self.is_thinking = False
        if self.StopButton:
            import System.Windows
            self.StopButton.Visibility = System.Windows.Visibility.Collapsed
        if self.SendButton:
            self.SendButton.IsEnabled = True
        self.replace_last_message("Operation cancelled by user.")

    def get_gemini_response(self, prompt):
        responded = False
        try:
            # All imports are now pre-loaded at the top of script.py
            client.log("Background Thread: Starting orchestration...")
            
            if not bridge._uiapp:
                client.log("Background Thread Error: bridge._uiapp is None.")
                err = "Error: Revit connection lost. Please click 'Start Server' again."
                from System import Action
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished(err)))
                return

            from revit_mcp.progress_tracker import BuildProgressTracker
            tracker = BuildProgressTracker(callback=self.update_progress)
            response = orchestrator.run_full_stack(bridge._uiapp, prompt, tracker=tracker)
            responded = True
            
            client.log("UI Thread: Response received.")
            from System import Action
            self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished(response)))
            
        except Exception as e:
            import traceback
            err_msg = "UI Thread CRASH: {}\n{}".format(str(e), traceback.format_exc())
            try: client.log(err_msg)
            except: pass
            print(err_msg)
            from System import Action
            self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished("Error: check log for details.")))
        finally:
            if not responded and not self.cancelled:
                # Fallback for unexpected thread termination
                from System import Action
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished("Error: Request failed.")))

    def on_response_finished(self, response):
        """Handle finished response (success or error)."""
        if self.cancelled: return
        self.is_thinking = False
        if self.StopButton:
            import System.Windows
            self.StopButton.Visibility = System.Windows.Visibility.Collapsed
        if self.SendButton:
            self.SendButton.IsEnabled = True
        self.replace_last_message(response)

    def replace_last_message(self, new_text):
        """Replace the last message (Thinking...) with actual response."""
        if self.ChatHistory and self.ChatHistory.Children.Count > 0:
            last_border = self.ChatHistory.Children[self.ChatHistory.Children.Count - 1]
            if isinstance(last_border.Child, TextBox):
                last_border.Child.Text = new_text
                
                # Update background based on content for visual feedback
                if new_text.startswith("Success:"):
                    last_border.Background = SolidColorBrush(Color.FromRgb(34, 139, 34)) # Green
                elif new_text.startswith("Error:"):
                    last_border.Background = SolidColorBrush(Color.FromRgb(178, 34, 34)) # Red
                
                # Update history too
                if len(self.history) > 0:
                    self.history[len(self.history)-1]["text"] = new_text

    def update_progress(self, msg):
        """Thread-safe progress update -- replaces the 'Thinking...' bubble text.
        Uses Invoke (not BeginInvoke) so the UI actually repaints during the build.
        """
        from System import Action
        if self.window.Dispatcher.CheckAccess():
            # Already on the WPF/Revit thread — update directly and pump messages
            self._apply_progress(msg)
            if _has_doevents:
                try:
                    WinForms.DoEvents()
                except:
                    pass
        else:
            # Background thread — marshal synchronously
            self.window.Dispatcher.Invoke(Action(lambda: self._apply_progress(msg)))

    def _apply_progress(self, msg):
        """Must run on WPF dispatcher thread."""
        if self.cancelled or not self.is_thinking:
            return
        if self.ChatHistory and self.ChatHistory.Children.Count > 0:
            last_border = self.ChatHistory.Children[self.ChatHistory.Children.Count - 1]
            if isinstance(last_border.Child, TextBox):
                last_border.Child.Text = msg
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()

    def on_key_down(self, sender, e):
        """Handle Enter key press to send message."""
        if e.Key == System.Windows.Input.Key.Enter:
            self.on_send_click(sender, e)

    def test_bridge(self):
        """Diagnostic tool to verify Revit bridge health - run in thread to avoid UI hang."""
        def run_test():
            try:
                client.log("Background Thread: Manual Bridge Ping started.")
                def ping_action(): return "PONG"
                # This now waits for the Timer to pick it up
                res = bridge.mcp_event_handler.run_on_main_thread(ping_action)
                
                client.log("Background Thread: Bridge Ping SUCCESS: " + str(res))
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.add_message("Bridge Status: ACTIVE (PONG received)", is_user=False)))
            except Exception as e:
                import traceback
                err = "Bridge Ping FAILED: {}\n{}".format(str(e), traceback.format_exc())
                client.log(err)
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.add_message("Bridge Status: OFFLINE\n" + str(e), is_user=False)))
        
        t = threading.Thread(target=run_test)
        t.daemon = True
        t.start()

    # Proxy methods for the main loop
    def Show(self, uiapp): 
        # Set Revit as the owner of the window to allow keyboard input
        self._uiapp = uiapp
        helper = WindowInteropHelper(self.window)
        helper.Owner = uiapp.MainWindowHandle
        
        try:
            ElementHost.EnableModelessKeyboardInterop(self.window)
        except:
            pass
            
        self.window.Show()
        self.window.Activate()
        
        # START THE IDLING PUMP (Provides valid API context for Transactions)
        try:
            self._uiapp.Idling += bridge.idling_handler
            client.log("UI: Idling Pump subscribed (v9-FINAL).")
        except Exception as e:
            client.log("UI: Idling subscription error: " + str(e))

    def Close(self):
        try:
            if self._uiapp:
                self._uiapp.Idling -= bridge.idling_handler
                client.log("UI: Idling Pump unsubscribed.")
        except:
            pass
        self.window.Close()
    @property
    def Visibility(self): return self.window.Visibility

def pump():
    """Pump Windows messages to keep STA message queue clear."""
    if _has_doevents:
        try:
            WinForms.DoEvents()
        except:
            pass

def main():
    # We DO NOT delete modules starting with 'revit_mcp' here.
    # This ensures the background thread and this UI script share the same module objects.
    output = script.get_output()
    output.close_others(all_open_outputs=True)

    try:
        from revit_mcp.runner import start_mcp_server
        success = start_mcp_server()
    except Exception as e:
        import traceback
        output.print_md("## ❌ Failed to Start")
        output.print_md("**Error:** `{}`\n```\n{}\n```".format(str(e), traceback.format_exc()))
        return

    if not success:
        output.print_md("## ℹ️ Server already active on Port 8001.")
    else:
        output.print_md("## ✅ Gemini MCP Server: Active")
        output.print_md("- **Port:** 8001")
        output.print_md("- **Inspector:** `http://localhost:8001/sse`")
        output.print_md("- **DoEvents pump:** `{}`".format("✅ Active" if _has_doevents else "⚠️ Unavailable"))
    
    output.print_md("---")
    output.print_md("⚠️ **Minimize** this window — do NOT close it (it keeps the server alive).")

    uiapp = HOST_APP.uiapp

    if not _init_success:
        output.print_md("## ❌ Plugin components failed to load.")
        output.print_md("Check your installation and try clicking 'Start Server' again.")
        output.print_md("---")
        output.print_md("### Error Details:")
        output.print_md("```\n{}\n```".format(_init_error))
        return

    # Initialize and show the chat window
    # Cleanup OLD windows if they exist
    global _current_chat_window
    if _current_chat_window:
        try:
            from revit_mcp.gemini_client import client
            client.log("Closing existing Chat Window...")
            _current_chat_window.Close()
        except Exception as e:
            import traceback
            from pyrevit import forms
            forms.alert("Error closing existing chat window: {}\n\n{}".format(str(e), traceback.format_exc()))
            
    xaml_file = os.path.join(os.path.dirname(__file__), "chat_ui.xaml")
    _current_chat_window = AIChatWindow(xaml_file)
    _current_chat_window.Show(uiapp) 

    # INITIALIZE BRIDGE (CRITICAL for non-blocking UI)
    from revit_mcp.bridge import init_bridge
    init_bridge(uiapp)
    
    # Alert removed to improve startup UX

    output.print_md("🚀 **UI Active.** (Keep this window open to maintain AI connection)")
    output.print_md("---")
    output.print_md("> [!TIP]\n> You can now use the Chat Window while Revit is running. The server will process your requests in the background.")

if __name__ == '__main__':
    main()
