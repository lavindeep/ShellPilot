# VHS tape for the README demo: question -> plan -> approval -> edit -> verification.
# Run from the repo root with a live Ollama + gemma4:e4b:
#   vhs docs/assets/demo.tape
# The demo workspace is prepared by scripts/make_demo_workspace.sh equivalent:
#   /tmp/sp-demo with greeting.py (returns "hello") and test_greeting.py (expects "howdy").

Output docs/assets/demo.gif

Set FontSize 15
Set Width 1050
Set Height 620
Set TypingSpeed 40ms
Set Framerate 12
Set PlaybackSpeed 3.0
Set Shell bash

Hide
Type "source /Users/lavin/Projects/ShellPilot/.venv/bin/activate && cd /tmp/sp-demo && clear"
Enter
Show

Type "shellpilot"
Enter
Sleep 8s

Type "test_greeting.py fails. Plan a fix for greet() in greeting.py so the test passes, then verify with pytest."
Enter
Sleep 60s

Type "y"
Enter
Sleep 90s

Type "y"
Enter
Sleep 90s

Type "/exit"
Enter
Sleep 4s
