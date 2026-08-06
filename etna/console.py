#--------------------------------------------------------------------------------------------------------------
#   etna/console.py — ANSI color, cursor control, and progress bar utilities
#   Originally simplify.py by dwhite-sys, adapted for Etna
#--------------------------------------------------------------------------------------------------------------

import sys
import time

#--------------------------------------------------------------------------------------------------------------
#   Functions
#--------------------------------------------------------------------------------------------------------------

def wait(duration=0.1):
    "Waits a specified time."
    time.sleep(duration)

def clear():
    "Clears the console."
    print("\033[H\033[J", flush=True)

def hide_cursor():
    "Hides the console cursor."
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

def show_cursor():
    "Unhides the console cursor."
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

colors = {
    "green":        "\033[32m",
    "light_green":  "\033[92m",
    "red":          "\033[31m",
    "white":        "\033[0m",       # terminal default/reset
    "yellow":       "\033[33m",
    "bright_yellow":"\033[93m",
    "cyan":         "\033[36m",
    "blue":         "\033[34m",
    "light_blue":   "\033[94m",
    "light_grey":   "\033[37m",      # distinct from white (default) and grey (dark)
    "grey":         "\033[90m",
    "purple":       "\033[35m",
    "orange":       "\033[38;5;208m",
}

green, light_green, red, white, yellow, bright_yellow, cyan, blue, light_blue, light_grey, grey, purple = (
    colors["green"], colors["light_green"], colors["red"], colors["white"],
    colors["yellow"], colors["bright_yellow"], colors["cyan"], colors["blue"],
    colors["light_blue"], colors["light_grey"], colors["grey"], colors["purple"],
)
orange = colors["orange"]

cursor_control = {
    "up":         "\033[A",
    "down":       "\033[B",
    "right":      "\033[C",
    "left":       "\033[D",
    "next_line":  "\n",
    "prev_line":  "\033[F",
    "clear_line": " \x1b[2K\r"
}
up, down, right, left, _, _, clear_line = cursor_control.values()

lines_wanted = 50

def progress_bar(progress: int, length: int, separate: bool = False):
    "Returns a loading string. If separate=True, returns (dots, percent, bar, end) tuple."
    dots = '.' * (progress % 4)
    if length == 0:
        pct = 0
    else:
        pct = int((progress / length) * 100) if progress != 0 else 0
    filled = int((progress / length) * lines_wanted) if length > 0 else 0
    bar = f'{light_green}{"█" * filled}{green}{"░" * (lines_wanted - filled)}'
    if not separate:
        return f"{dots.ljust(4)} {str(pct).ljust(2)}% {bar} ", f'{white}\r'
    else:
        return f"{dots.ljust(4)}", f"{str(pct).ljust(2)}%", f"{bar} ", f'{white}\r'

# ── Throbber ─────────────────────────────────────────────────────────────────

THROBBER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

def throbber(tick: int) -> str:
    "Returns the throbber frame for the given tick count."
    return THROBBER_FRAMES[tick % len(THROBBER_FRAMES)]

# ── Etna-specific ─────────────────────────────────────────────────────────────

PREFIX = f"{light_blue}[Etna]{white} "
