Output docs/demo.gif

Set Shell zsh
Set FontSize 13
Set Width 1100
Set Height 680
Set Theme "Dracula"
Set Padding 20
Set TypingSpeed 60ms
Set PlaybackSpeed 0.8

Hide
  Type "source /Users/hongjun/work/proliant-cli/.venv/bin/activate"
  Enter
  Sleep 500ms
Show

Type "proliant ilo firmware list"
Sleep 500ms
Enter
Sleep 6s

Type "proliant com devices list"
Sleep 500ms
Enter
Sleep 6s

Type "proliant qs list --model dl380gen12"
Sleep 500ms
Enter
Sleep 8s

Sleep 3s
