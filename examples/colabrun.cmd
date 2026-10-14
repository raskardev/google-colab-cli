@echo off
rem colabrun.cmd - Windows stand-in for the "#!/usr/bin/env -S colab run" shebang. Usage: colabrun script.py [args...]
colab run %*
