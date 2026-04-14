"""Test pynput — check if Ctrl+Win is detected"""
from pynput import keyboard

print("  pynput key test — press Ctrl+Win, then Ctrl+C to quit")
print()

current_keys = set()

def on_press(key):
    current_keys.add(key)
    name = getattr(key, 'name', None) or getattr(key, 'char', str(key))
    ctrl = any(k in current_keys for k in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl))
    win = any(k in current_keys for k in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r))
    print(f"  DOWN: {str(name):20s} | ctrl={ctrl}  win={win}")
    if ctrl and win:
        print("  >>> CTRL+WIN COMBO DETECTED <<<")

def on_release(key):
    current_keys.discard(key)
    name = getattr(key, 'name', None) or getattr(key, 'char', str(key))
    print(f"  UP:   {str(name):20s}")

with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
