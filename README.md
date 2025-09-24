![GitHub stars](https://img.shields.io/github/stars/TheCascadian/HOI4FocusGUI?style=flat-square)
![GitHub forks](https://img.shields.io/github/forks/TheCascadian/HOI4FocusGUI?style=flat-square)
![GitHub watchers](https://img.shields.io/github/watchers/TheCascadian/HOI4FocusGUI?style=flat-square)

> DISCORD LINK - Prophet's Preposterous Projects: https://discord.gg/RuaR7CBy7Z

> Original Reddit Post - https://www.reddit.com/r/hoi4modding/comments/1nmkbg5/focus_tree_editor_tool

<span style="color: White; font-size: 4em; font-weight: bold; text-shadow: 5px 5px 3px black;">HOI4 Focus GUI</span>
- Python-based Hearts of Iron IV mod GUI, primarily built for generating random focus trees. 


<span style="color: White; font-size: 3em; font-weight: bold; text-shadow: 5px 5px 3px black;">FEATURES*</span>*<ins>may change at ANY time<ins>

<span style="color: White; font-size: 1.5em; font-weight: bold; text-shadow: 5px 5px 3px black;">Project Manager</span>

![alt text](images/project_manager.PNG)

![alt text](images/app_folders.png)

<span style="color: White; font-size: 1.5em; font-weight: bold; text-shadow: 5px 5px 3px black;">Confirmation Popups</span>

![alt text](images/loaded_project_dialog.PNG)

<span style="color: White; font-size: 1.5em; font-weight: bold; text-shadow: 5px 5px 3px black;">Editor Canvas</span>

![alt text](images/project_canvas.PNG)

<span style="color: White; font-size: 1.5em; font-weight: bold; text-shadow: 5px 5px 3px black;">Toolbar</span>

![alt text](images/main_toolbar.png)

<span style="color: Orange; font-size: 2.1em; font-weight: bold; text-shadow: 3px 3px 3px black;">USER WARNING (Norton AV)</span>
- This will likely be flagged as a virus -- mainly because it is an .exe file made with the use of PyInstaller via AutoPyToExe. Also, I cannot afford to certify this app. 
- If you still want to use it: on the right side of the options for it, there's "Restore", and "Add Exclusion and Restore", click the second option. This should allow you to use the app.

<span style="color: Yellow; font-size: 2.1em; font-weight: bold; text-shadow: 3px 3px 3px black;"><ins>PLEASE READ<ins></span><span style="color: Yellow; font-size: 2.1em;">!</span>
1. Save frequently. There is no auto-save feature (so far). Backup your files in another folder when exporting to HOI4 code.
2. Toolkit is still in very early development. Paradox are prone to breaking changes in modding, so, if you notice something out of the ordinary, feel free to stop in to my discord and let me know what's going on!

<span style="color: White; font-size: 3em; font-weight: bold; text-shadow: 5px 5px 3px black;">Peace of Mind</span>
--
- All data is stored locally, on YOUR PC, forever (until deleted/removed/transferred).
- No server setup, no installer (also means no uninstaller, everything is held here: AppData\Local\FocusTool -- EXCEPT the .exe itself).

For technically-inclined users: a list of Python Packages which were used in the creation of the application for your review.

#### Standard Library Packages:
- `os`, `re`, `sys`, `time`, `json`, `uuid`, `typing`, `logging`, `datetime`, `threading`, `subprocess`
#### Additional Packages:
- `PIL`, `PyQt6.QtCore`, `PyQt6.QtGui`, `PyQt6.QtWidgets`