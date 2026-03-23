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

lib_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib'))
if lib_path not in sys.path:
    sys.path.append(lib_path)

from pyrevit import script, forms, HOST_APP
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
from System.Windows.Markup import XamlReader
from System.IO import FileStream, FileMode, FileAccess, FileShare
from System.Windows.Interop import WindowInteropHelper

# Add reference for keyboard interop
clr.AddReference("WindowsFormsIntegration")
from System.Windows.Forms.Integration import ElementHost

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
        if not self.UserInput: return
        user_text = self.UserInput.Text.strip()
        if user_text:
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
            thread = threading.Thread(target=self.get_gemini_response, args=(user_text,))
            thread.daemon = True
            thread.start()

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
        """Call Gemini API and update UI with response."""
        responded = False
        try:
            from revit_mcp.gemini_client import client
            client.log("Thread: Calling Gemini for prompt: {}".format(prompt[:30]))
            response = client.chat(prompt, history=self.history[:-1])
            client.log("Thread: Gemini returned: {}".format(str(response)[:50]))
            responded = True
            # Update UI on the UI thread
            from System import Action
            self.window.Dispatcher.Invoke(Action(lambda: self.on_response_finished(response)))
        except Exception as e:
            if self.cancelled: return
            err_msg = "Error: " + str(e)
            try:
                from revit_mcp.gemini_client import client
                client.log("Thread Exception: " + err_msg)
            except: pass
            responded = True
            from System import Action
            self.window.Dispatcher.Invoke(Action(lambda: self.on_response_finished(err_msg)))
        finally:
            # Safety net: only fires if no response was dispatched above
            if not responded and not self.cancelled:
                from System import Action
                try: self.window.Dispatcher.Invoke(Action(lambda: self.on_response_finished("Error: Request timed out or failed unexpectedly.")))
                except: pass

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

    def on_key_down(self, sender, e):
        """Handle Enter key press to send message."""
        if e.Key == System.Windows.Input.Key.Enter:
            self.on_send_click(sender, e)

    # Proxy methods for the main loop
    def Show(self, uiapp): 
        # Set Revit as the owner of the window to allow keyboard input
        helper = WindowInteropHelper(self.window)
        helper.Owner = uiapp.MainWindowHandle
        
        # FIX: Enable keyboard interop for modeless WPF window in Revit
        # This allows the window to receive keyboard focus correctly.
        try:
            ElementHost.EnableModelessKeyboardInterop(self.window)
        except:
            pass
            
        self.window.Show()
        self.window.Activate()

    def Close(self): self.window.Close()
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

    # Initialize and show the chat window
    xaml_file = os.path.join(os.path.dirname(__file__), "chat_ui.xaml")
    chat_window = AIChatWindow(xaml_file)
    chat_window.Show(uiapp) 

    # INITIALIZE BRIDGE (CRITICAL for non-blocking UI)
    from revit_mcp.bridge import init_bridge
    init_bridge(uiapp)

    output.print_md("---")
    output.print_md("🚀 **UI Unblocked.** You can now select elements and edit normally.")
    output.print_md("The AI Server is running in the background.")

if __name__ == '__main__':
    main()
