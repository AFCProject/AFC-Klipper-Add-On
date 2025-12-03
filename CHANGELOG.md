# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2025-11-04]
### Changed
- Updated error message when using the SET_LANE_LOADED command to be more descriptive.

## [2025-11-03]
### Fixes
- Clarified fix message if during AFC calibration, the filament fails to reach to hub sensor.

## [2025-11-01]
### Changed
- Removed code for Belay.
- Cleanup terminology around compressing/expanding of the buffer to make it easier to understand for users.

## [2025-10-29]
### Fixes
- Fixed klipper crashing when commanding distance of zero for LANE_MOVE macro.

## [2025-10-25]
### Fixes
- Resolved a bug where runout logic could potentially be triggered during a toolchange.
- Resolved a bug where AFC_STATUS would crash klipper when using buffer as toolhead sensor and last lane was loaded into toolhead.

## [2025-10-18]
### Fixes
- On startup, or when assigning a spool to a lane, AFC will now check the weight of the spool to check if it is either zero, null,
  or a negative value. If any of these conditions are met, AFC will not assign the spool. This check can be disabled by 
  setting `disable_weight_check: True` in the `[AFC]` section of the `AFC.cfg` file.

## [2025-10-16]
### Fixes
- Fixed issue with debounce logic on latest version of Kalico.

## [2025-10-12]
### Fixes
- Capitalized AFC_CALIBRATION help text
- Removing returning TD-1 color as color in api endpoint, TD-1 color is still returned in td1_color variable per lane
- Current toolchange will return zero if current toolchange is below zero(starts at -1 when first starting a print)
- Added additional logic when parsing TD-1 scan_time to work with updated format in moonraker

## [2025-10-10]
### Added
- Created a new folder for community-contributed mods and configurations at ``/community_mods/``
- Added Blurolls AFC-X mcu board with a path of ``/community_mods/mcu/AFC-X.cfg``. [Customer image of board](https://ae-pic-a1.aliexpress-media.com/kf/A030fad34724c426ba8564ca98bb570dfQ.jpg_.webp) Colors do **NOT** match the product description on online retailers.

## [2025-09-30]
### Added
- Allow `tool_stn_unload` to be `0` for toolheads with cutter above extruder.

## [2025-09-26]
### Added
- Support to move filament to TD-1 device that is inline with PTFE tube to gather TD and color

## [2025-09-27]
### Fixes
- Logging the same information multiple times to AFC.log file

## [2025-09-07]
### Added
- Support to push lane information to moonrakers `machine/lane_data` endpoint so that third-parties can pull this information easily(eg. orcaslicer)

### Fixes
- The `AFC_LANE_RESET` macro will properly check for input instead of crashing Klipper.
### Added
- Added ability to auto level when `auto_level_macro` is defined with a valid leveling macro.

## [2025-09-05]
### Added
- Check to verify that pin_tool_start/end is not set to `Unknown`, throws error if pins are set to `Unknown`.
### Fixes
- Compatibility issue with klipper after version v0.13.0-190-g5eb07966

## [2025-09-01]
### Fixes
- Issue with older klipper version before debounce button was added

## [2025-08-30]
### Added
- Catch for JSON decode error when trying to read and load AFC.var.unit file

### Fixes
- Issue where TMC section search would error out if not defined. Search is now gated behind user enabling print_current variable. If a user is using a different driver, like a4988 for example, AFC will not error out as long as print_current variable is not defined.

## [2025-08-24]
### Added
- You can now use the `install-afc.sh` script to delete the `AFC.var.unit` file if necessary. This option is located under
  the `Utilities` menu.

### Fixed
- Issue where HTLF unit would not select lane and sync with extruder when running prep

## [2025-08-22]
### Added
- Added a new command to test lane loading and unloading in an automated and random fashion (`AFC_TEST_LANES`). Please
  see the documentation for more information on how to use this command and it's various options.
- Option to delay/debounce switches. Debounce delay is defaulted to zero but can be updated globally by adding `debounce_delay: <delay_value>`
  to AFC config section. Or this value can be added per AFC_extruder, AFC_hub, AFC_stepper/AFC_lane configs. Runout can be also disabled by
  turning off filament switch in gui, if PREP sensor is disabled this will also disable infinite spool rollover. When klipper is restarted
  all switches will be enabled again.

## [2025-08-17]
### Added
- Servo option to brush macro.

## [2025-08-10]
### Fixed
- A negative `afc_unload_bowden_length` is no longer able to be set by the calibration routine. 

## [2025-08-06]
### Fixed
- The `install-afc.sh` script will no longer tell you it removed the `velocity` setting if it didn't exist.

## [2025-07-30]
### Fixed
- Updated the `SET_MAP` command to correctly handle `MAP` parameters in either upper or lower case text. 

## [2025-07-27]
### Updated
- The `install-afc.sh` script will now only copy relevant MCU files when installing a new unit. 

## [2025-07-20]
### Added
- Software defined physical buttons are now available and supported. See documentation for more information on how to set them up.
- New command `SET_NEXT_SPOOL_ID` to be used with a QR scanner tool or macro that automatically sets the id of the next spool loaded.
- Support setting tool_max_unload_attempts to zero to bypass buffer unloading checks

## [2025-07-19]
### Fixes
- Error with infinite spool where klipper would crash if runout was set to `None` instead of `"NONE"`
- Added a check when enabling virtual bypass to make sure a lane is not loaded when enabling.
- Issue where localhost and http were hardcoded, allows user to specify custom url. Fixes issue 484.
- Updated code to inform users when trying to assign spoolman ID to a lane and that same spool ID is already assigned to another lane.

## [2025-07-06]
### Fixes
- Race condition between klipper and moonraker when trying to get stats from moonraker database

## [2025-06-30]
### Fixes
- Issue #476 where turn off led macro didn't turn off LEDs while printing
- TTC's that some users were having that was induced by commit `1201bcc`

## [2025-06-28]
### Updated
- The `install-afc.sh` script will now display the version when an update is completed.

## [2025-06-23]
### Added
- Runout/break/jam detection for hub and toolhead sensors:
- If the toolhead or hub sensor detects runout but upstream sensors still detect filament, the print is paused and the user is notified of a possible break/jam (no eject or endless spool mode is attempted).
- Runout/pause logic only triggers during normal printing states, preventing false positives during lane load/unload or filament swaps.
- `handle_toolhead_runout` and `handle_hub_runout` methods added to `AFCLane` for special handling of break/jam scenarios at the toolhead and hub.
- Hub sensor callback now calls `handle_hub_runout` on all associated lanes when runout is detected.

### Changed
- Enhanced runout logic in `AFC_lane.py`, `AFC_extruder.py`, and `AFC_hub.py` to support multi-sensor and break/jam detection.

### Fixed
- Addresses issue [#389](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues/389) and [#387](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues/387)
- Added cutting direction check to _MOVE_TO_CUTTER_PIN. This prevents crashes when using front/back cutting motion.

## [2025-06-21]
### Fixed
- Ensure `default_material_temps` name matching in temperature selection logic is case-insensitive.

## [2025-06-19]
## Updated
- The `afc-debug.sh` script will now also include the `moonraker.conf` file if it is present.

### Added
- Added an option to disable skew_correction for kinematic moves.
- AFC now errors out when using buffer as toolhead sensor and it fails to decompress when loading/unloading.

## [2025-06-18]
## Updated
- RESET_AFC_MAPPING function to reset manually set lane mapping in config to correct lane

## Fixed
- Fixed function that auto assigns T(n) commands to check and verify that other T(n) commands are not already registered outside of AFC

## [2025-06-17]
### Fixed
- Updated `cycles_per_rotation` value to be less aggressive at 800 for print assist
- Updated `enable_assist_weight` value to be 500 so print assist start once weight gets below 500 grams

## [2025-06-16]
### Removed
- Removed the version checking functionality for force updates from the `install-afc.sh` script. 

### Fixed
- Issue where espoolers would move way faster than normal when weight was below empty spool weight.

## [2025-06-15]
### Added
- Added support for the AFC-Pro board in the installer to install an 8-Lane Boxturtle.
- The `RESET_AFC_MAPPING` macro will now also reset any runout lane configurations.

## [2025-06-10]
### Added
- Ability to turn on print assist if spool falls below a certain weight
- Weight defaults to 1kg when first inserting spool
- `AFC_CLEAR_MESSAGE` macro to clear current message that would be displayed in gui's
- When saving variables and key is not found in current AFC files, a new file `AFC_auto_vars.cfg` will be created and variables will be added to that file

## [2025-06-08]
### Added
- Support for [QuattroBox](https://github.com/Batalhoti/QuattroBox) filament changer. QuattroBox can be chosen in install script for new or additional units to add to your printer

## [2025-06-07]
### Fixed
- `unknown command: Prompt_end` error will no longer show when users try to exit out of Happy Printing popup after AFC_CALIBRATION is done
- Lanes are now marked a loaded_to_hub when bowden calibration happens
- Fixed issue where HTLF might error out when first homing during PREP

## [2025-06-06]
### Added
- There is now a configurable option `error_timeout` in the `[AFC]` section of the `AFC.cfg` file. This option allows 
users to set a timeout for how long the printer will stay in a paused state when an error occurs. The default value is 
`36000` seconds (10 hours). When a `PAUSE` action is triggered, AFC will now compare the value of the `error_timeout` and
the `idle_timeout` value (if defined) and choose the larger of the two values. 

### Changed
- The `afc-debug.sh` script will now create a zip file of the logs if the `nc` utility is not available. 


## [2025-05-30]
### Added
- Updated park to allow moving to an absolute z height after the x,y move. This is intended to reduce oozing during unload and load prior to using the poop command.

## [2025-05-29]
### Fixed
- HTLF infinite runout now works correctly

## [2025-05-27]
### Added
- Some AFC macros are now exposed in Mainsail/Fluidd. 

## [2025-05-25]
### Fixed
- Exclude object bug where klipper would error out with max extrude error after excluding an object and 
  trying to do a lane swap or doing TOOL_UNLOAD in PRINT_END function. Fixes issue [#364](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues/364)
- Fixing issue [#348](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On/issues/348)

## [2025-05-24]
### Fixed
- The calibration routines will now not allow a negative bowden length value to be set. If a negative value is detected, 
an error message will be displayed and the value will not be set.

## [2025-05-23]
### Updated
- The `PREP` sequence will now check to ensure the trailing and advance buffer switches are not both triggered. If 
  both switches are triggered, a warning message will be displayed.

## [2025-05-22]
### Added
- Added `auto_home` support,

## [2025-05-22]
### Added
- Added statistics tracking for tool load/unload/total change, n20 runtime, number of cuts,
  average load/unload/full toolchange times, and number of load per lane.
- Added ability to track when last blade was changed and how many cuts since last changed
- `AFC_STATS` macro added to print statistics out. Set `SHORT=1` to print out a skinny version
- `AFC_CHANGE_BLADE` macro added for when users change blade as this reset count and updates date changed
- `AFC_RESET_MOTOR_TIME` macro added to allow users to reset N20 active time if motor was replaced in a lane
- Added common class for easily interacting with moonraker api
- Updated to use moonrakers proxy when fetching spoolmans data
- Added getting toolchange count from moonrakers file metadata, `SET_AFC_TOOLCHANGES` will be deprecated
  Moonrakers version needs to be at least v0.9.3-64
- Updated import error message to pull from a common error string in AFC_utils.py file
- Clearing pause in klipper when starting a print
- Warning message is outputted when number of cuts is within 1K of tool_cut_threshold value
- Error message is outputted when number of cuts is over tool_cut_threshold

### Fixed
- Issue where virtual bypass was being set for newly installed instances of AFC

## [2025-05-21]
### Added
- new macro `AFC_TOGGLE_MACRO` to enable disable other macros.

## [2025-05-15]
### Added
- added quiet mode support. `quiet_moves_speed` on `AFC.cfg` dictates the max speed when quiet mode is enabled.
- new macro `AFC_QUIET_MODE ENABLE=1/0 SPEED=<max_speed>` to allow modifying `quiet_moves_speed` and enable/disable quiet mode.

## [2025-05-12]
### Added
- new variable `tool_homing_distance` in `[AFC]` to make the distance over which toolhead homing is attempted.
- new variable `rev_long_moves_speed_factor` added to `AFC_lane` to allow per lane reverse speed for long moves. i.e. long move speeds will now be `rev_long_moves_speed_factor * long_move_speed`.
- new macro `SET_LONG_MOVE_SPEED LANE=<lane_name> FWD_SPEED=<fwd_speed> RWD_FACTOR=<rwd_multiplier> SAVE=1/0` to allow modifying `rev_long_moves_speed_factor` and `long_move_speed`


## [2025-05-11]
### Changed
- The `install-afc.sh` script will remove any `velocity` settings present in the `[AFC_buffer <buffer_name>]` 
  section of the configuration files as they are no longer needed.

## [2025-05-01]
### Added
- Print assist is now filament usage based and will activate spool after a specified amount of filament is used. This is enabled by default.

### Removed
- Removed velocity from AFC_buffer code and install code, please remove `velocity`  variable from AFC_buffer configuration

## [2025-04-27]

### Removed
- Removed deprecated belay code.

## [2025-04-25]
### Added
- The AFC_CUT macro now supports a servo-activated pin. Set values for ``[servo tool_cut]`` in ``AFC_Hardware.cfg`` and enable ``tool_servo_enable`` in ``AFC_Macro_Vars.cfg``

## [2025-04-23]

### Added
- The `install-afc.sh` script will now prompt you if you want to update the AFC provided macros when updating the 
  software. **WARNING** This will overwrite any existing macros present. 

## [2025-04-20]

### Changed
- Updated poop to do z lift based off last position so that toolhead does not smash into large poops.
- Updated kick to move xy first and then move z so toolhead does not smash into poop.

## [2025-04-19]

### Changed
- The `Type` parameter in the `AFC_<unit_type>.cfg` file is no longer required.

- The `install-afc.sh` script will now check for updates, and if new updates are present, it will sync and git changes
and re-run the script automatically.

## [2025-04-12]

### Added
- The `afc-debug.sh` script will now also upload `AFC.log` files for assistance during troubleshooting.

### Changed
- All documentation is now available on our website at https://armoredturtle.xyz/docs/.

## [2025-04-09]

### Added
- Added check in prep to make sure printer is homed when using direct loading

### Fixed
- For direct loading, fixed logic to use load sensor for unloading and then retract back more
  to make sure filament was fully out of extruder gears
- Fixed error where start time was not correctly getting set for direct loads
- Fixed error where unsyncing lanes for HTLF units was still syncing back

## [2025-04-08]

### Added
- Function to check if in absolute mode and set absolute if in relative mode since
  AFC does movement base off being in absolute mode
- Runout/infinite spool support for HTLF unit type
  
### Fixed
- Fixed error where restore_pos was not calculating base position correctly for extruder,
  matched how RESTORE_STATE does it

## [2025-04-07]

### Fixed
- Fixed detection for python version check to appropriately check for both python minor and major version.

## [2025-04-06]

### Fixed
- Update kick macro to ensure we are in absolute position mode (G90) before doing moves

## [2025-04-06]

### Changed
- Updated wording for when `TOOL_UNLOAD` fails and filament is still in toolhead. Added instruction for user to run `UNSET_LANE_LOADED` before running the correct `T(n)` macro

### Fixed
- Issue when user tries to run `TEST` macro and `afc_motor_rwd` is not defined in config. Affects configs that don't use spooler motors.

### Removed
- Remove documentation for Belay support, it is deprecated and will be fully removed in a future release.

## [2025-04-01]

### Added
- Support for HTLF

### Fixed
- Error when user calls TOOL_UNLOAD outside a print and it fails to unload. Fixed error where variable was not set when creating message to printout to console

## [2025-03-30]
### Fixed
- The `BT_LANE_MOVE` macro now correctly only accepts positive or negative values for the `distance` parameter.

## [2025-03-29]
### Added
- The `install-afc.sh` script now has the ability to rename existing units.
- The `install-afc.sh` script now has the ability to install NightOwl units. Thanks to @thomasfjen for the contribution.
- The `install-afc.sh` script now has the ability to help install multiple units.

### Fixed
- The `install-afc.sh` script now correctly checked for a Python version >= Python 3.8.

## [2025-03-27]
### Added
- AWD variable to CUT macro so increased current applies to all X motors
- Updated cut variable retract to 20 and pushback to 15

### Fixed
- Resetting `in_toolchange` variable when resuming from failure, fixes problems with returning to correct z hight on the next in_toolchange
- Fixed issued with `AFC_reset` macro when distance was not supplied macro call would crash klipper

## [2025-03-22]
### Fixed
- Fixed possible error if hotend current temp is below current temp. 

## [2025-03-17]
### Added
- Added `SET_SPEED_MULTIPLIER` macro to allow user to change fwd/rwd speed multipliers during prints
- Added `SAVE_SPEED_MULTIPLIER` macro to save updated multiplier to config file for specified lane

### Fixed
- Added check to AFC pause/resume functions to make sure printer was not paused/paused before doing any actions
- Fixed issue where macro variables were not passed from AFC_PAUSE/AFC_RESUME to PAUSE/RESUME macros if user passed in variables when calling these macros  

## [2025-03-12]
### Added
- Virtual bypass sensor, AFC adds this sensor if hardware bypass is not detected

### Fixed
- Issue where z would move back down when calling cut macro after z hop from AFC

## [2025-03-10]
### Added
- Reporting error messages in AFC status so they can be shown in AFC integration panel

### Fixed
- Issue where resuming position could crash into object/purge tower
- Issue where creating filament_switch_sensor in AFC would cause klipper to error out when AFC include is before `[pause_resume]` and user has `recover_velocity` defined
- Issue where passing in `+-<number>` for length when calling `SET_BOWDEN_LENGTH` would crash klipper 

## [2025-03-07]
### Added
- Added variable_z_purge_move to Poop macro. Setting this to False will allow pooping with no z movement
- Added variable_z_move to brush macro. this value will set a positive Z move at the end of the brush to move the nozzle away from the brush

### Fixed
- Fixed error that occurs when all lanes are calibrated

## [2025-03-07]
### Added
- Added error checking to spool runout, before if a error happened during unload it could keep running the print
- Added lane ejection when runout detected but rollover not setup
- Added AFC_PAUSE function to override users pause macro so that necessary measures could be added to move in Z to avoid
  hitting part if users pause macro moves toolhead
- Added `afc_unload_bowden_length` parameter
- Added moving Z to previous saved position +z hop when resuming to avoid hitting part when moving back

### Fixed
- Fixed error when trying to turn of LEDs
- Fixed saving position as it was not saving correctly
- Reworked rollover logic to restore position after lane has been ejected fully so that nozzle does not sit
  on part while ejecting spool
- Fixed error where user could put wrong lane for rollover and it would not error until runout logic is triggered
- Fixed errors found in calibration routines


## [2025-03-02]

### Fixed
- Fixed calibration to error out at excessive distances
    - Calibration uses default config values plus fixed distances to be able to error out distances

## [2025-02-28]

### Added
- Logging of delta time and total time for how long toolchanges take
- Logging for AFC now logs to AFC.log file
- Ability to turn off/on AFC leds with `TURN_OFF_AFC_LED`/`TURN_ON_AFC_LED`
- `default_material_type` variable to assign to spool when loaded into lane
- `pause_when_bypass_active` variable to pause print if bypass is active, defaults to false
- `unload_on_runout variable` to unload lane when runout happens and another lane is not setup to change to, default to false
- Updated calibration to use buffer as tool_pin_start if only tool_pin_end is defined and buffer is also defined
- Ability to change tool_stn/tool_stn_unload/tool_sensor_after_extruder without restarting

### Fixed
- Issue where filament was not unloading correctly when only tool_pin_end is defined
- Issue where prep logic would try to unload forever if only tool_pin_end was defined

## [2025-02-25]

### Fixed

- Tip forming was multiplying all speeds with a factor of 60 by mistake. Existing configuration might need to be adapted
  to compensate for this fix.

## [2025-02-23]

### Added
- Checking to make sure lane was not None in cmd_CHANGE_TOOL
- Pauses in TOOL_LOAD/TOOL_UNLOAD/CHANGE_TOOL for early returns if printer is currently in a print

### Changed
- The `install-afc.sh` script will now check for a supported version of python and fail the installation if it is not present.

### Fixed
- Error in cmd_CHANGE_TOOL where change logic was being triggered if change was in a comment on the same line
- Turned runout pause message into error message which also pauses printer
- Error where infinite spool would crash klipper when calling change tool

## [2025-02-20]

### Added

- Users are now able to specify a non default moonraker address when using the `install-afc.sh` script. This value defaults to `http://localhost` but
can be adjusted for cases such as a remote moonraker installation, https, etc. This value can be set during the installation process by using the `-a <address>` flag.

## [2025-02-19]

### Added
- Clearing error_state when print starts, before this could be set before printing and would cause AFC to not save/restore position
- Added 1 second time debounce to prep callback
- Added abs function when determining speed for LANE_MOVE macro

### Changed
- Updated error print out messages when loading/unloading
- The way error messages printed out so they are grouped together

### Fixed
- Issue where getting spoolman data would error out when server variable in moonraker ended in a slash
- Issue where prep would no longer activate extruder motors when user rapidely triggered prep sensor

## [2025-02-17]

### Changed
- Updated the `install-afc.sh` script to prompt the user to install dependencies if they are not already installed instead of installing them automatically.

## [2025-02-16]

### Added
- Ability to manually set and unset lanes that are loaded in toolhead
- Braking to n20 when stopping them. This was advised to implement from Isik to hopefully help reduce backfeeding from motors into MCU board when in coast mode
- Default temperature value to default_material_temps list instead of using min_temp_val + 5
- Check for printing for LANE_MOVE, HUB_LOAD and LANE_UNLOAD macros
- Variable for prep done so save_vars function is not called before running prep function which would override the variables file before PREP could run
- More guidance to error messages when errors happen during TOOL_LOAD and TOOL_UNLOAD
- Helper function to get loaded lane for current extruder, help move the code towards working with multiple extruders
- Debounce logic when triggering prep sensor so that it does not run more than once
- Variable speed to LANE_MOVE move, run faster for distance over 200
- Printout when trying to load and load sensor is already triggered
- More printout to let user know when calibration is done
- Printout when trying to unload but no lane is loaded

### Changed
- Updated documentation

### Fixed
- Error in prep when there are multiple extruders
- Error when hub was not defined

## [2025-02-13]

### Added
- Assisted unload  
  When enabled, the retracts out of the toolhead before the long, fast move back throught the bowden tube is assisted.
  This helps with full spools where even a retract of a few centimeters can cause a loop to fall off the spool.

### Changed
- The `install-afc.sh` install script will now remove the `AFC.var.tool` file if detected as it is no longer needed.

## [2025-02-04]

### Changed
- Changed default `hub_clear_move_dis` to 25 to avoid too much retraction during filament changes

### Fixed
- Fixed error out during calibration when not calibrating bowden length
- Fixed issue where AFC could crash klipper in some scenarios when tool unloads fail to clear hub


## [2025-02-03]

### Changed
- Added expanded and compressed to buffer query for easier troubleshooting

### Fixed
- Corrected readme to point to the correct files
- Added check for key on bowden length calibration to not crash klipper if the wrong value is provided

## [2025-01-28]

### Changed
- Added `velocity` default setting back into buffer configuration when using the `install-afc.sh` script. This value was previously set to `0` by default,
but the configuration did not display this value. For future installations, this value will be explicitly set in the buffer configuration.

### Fixed
- Added minor documentation changes regarding `velocity` changes in the buffer configuration.

## [2025-01-26]
### Added
- Added ability to specify moonraker port, needed for when user has multiple moonraker/klipper instances on a single machine

### Fixed
- Fixed issue where software was updated to no long detected movement outside of printing, this fixes crashing klipper when inserting filament while printer is moving
- Fixed issue where remaining weight was not being pulled correctly from spoolman

## [2025-01-23]
### Fixed
- Fixed not being able to unload filament with UNLOAD_FILAMENT macro when using bypass

## [2025-01-17]
### Added
- Added ability to break up long bowden moves into shorter moves with `max_move_dis` variable to help with users that are facing timer too close issues when doing long moves. This variable can be set in `AFC.cfg` as a global setting or in the stepper/config sections.

### Changed
- Function `isPrinting` is determined by `print_stats` instead of `idle_timeout`
- Check extruder temp function only sets temperature if hotend can't extrude(temp below min value) and if printer is not printing, so that hotend temperature is not changed while printing

## [2025-01-12]
### Added

- Added Guided calibration using `AFC_CALIBRATION`
  - With the call of `AFC_CALIBRATION` the user will be guided through calibrating the lanes in their AFC unit.
- Added `UNIT` as an option to the `CALIBRATE_AFC` macro that is leveraged to calibrate lanes in a specific unit.

## [2025-01-12]
### Added
- Remapping stock `UNLOAD_FILAMENT` to call `TOOL_UNLOAD` function. Stock `UNLOAD_FILAMENT` will be renamed to `_AFC_RENAMED_UNLOAD_FILAMENT_` and can still be called from the command line. If trying to do a `TOOL_UNLOAD` and filament is loaded into bypass, AFC will unload with `_AFC_RENAMED_UNLOAD_FILAMENT_` macro. Remapping `UNLOAD_FILAMENT` macro can be disabled by setting `disable_unload_filament_remapping: True` in AFC_prep config section. 
- Added `docs/CONFIGURATION_OPTIONS.md` file that describes the different config parameters, still a work in progress

### Changed
- AFC-Klipper-Add-On now pulls spoolman ip/port from moonracker.conf file, please remove `spoolman_ip` and `spoolman_port` from `AFC/AFC.cfg` file


## [2025-01-11]
### Added
- Added clearing spool info and `load_to_hub` when lane retracts too far past Box Turtle extruder, reset once prep goes low
- Added logic so that buffer/extruder/hubs don't need to be entered per stepper as long as they are defined in their Unit(AFC_BoxTurtle/AFC_NightOwl) section 

For the following please see [Features doc](docs/Features.md) for more information on how to setup
- Added ability to set and track number of toolchanges when doing multicolor prints
- Added ability to set extruder temperature based off spoolman values or filament materials type if manually entered
- Added ability to show sensors as filament sensors in gui's
- Added ability to use multiple buffers
- Added automatically loading filament to hub for users that have moved their hubs closer to their toolhead

## [2025-01-10]

### Changed
- Due to a too long retraction, hub cuts had the risk of ejecting filament from the extruder,
  requiring manual intervention. The hub cut sequence was changed to avoid this situation.

  **NOTE**: due to the new way hub cuts are performed, the configuration has to be updated!
        The value `cut_dist` in `[AFC_Hub]` has to be reduced by about 150. Please recalibrate
        this before the next print.

### Added
- [AFC_Hub] has a new config option: `assisted_retract`. If set to true, retracts are assisted so
  that filament can't get loose on the spool.

### Changed

- `http://<ip_address>/printer/objects/query?AFC` has moved to `http://<ip_address>/printer/afc/status`endpoint. If tools have been designed around original endpoint please review results returned as new items have been added

## [2025-01-08]
### Added
- If using `tool_end` variable(sensor after extruder gears) `tool_stn` distance will now be based off this sensor
- When running `CALIBRATE_AFC` command values will be automatically updated and saved to config file

### Fixed
- Fixed infinite spool logic

## [2025-01-05]

### Updated
- This update will require the re-installation of an user's configuration  files due to changes in the config file structure.
- Re-designed the `install-afc.sh` script to be more user-friendly, interactive, and more indicative of what is happening 
during the installation process.

## [2024-12-29]

### Added
- **New Command: `GET_TIP_FORMING`**
  Shows the current tip forming configuration. Mostly interesting together with
  SET_TIP_FORMING.

- **New Command: `SET_TIP_FORMING`**
  Allows to update tip forming configuration at runtime.

  See command_reference doc for more info

## [2024-12-23]
### Added
- Added ability to set lower stepper current when printing to help reduce how hot steppers can get.
  To enable this feature set `global_print_current` in AFC.cfg or `print_current` for each AFC_stepper
  During testing it was found that 0.6 was optimal, going lower than this may result in buffer not working as intended

- Added check to make sure printer is not printing or homing when trying to load a spool. Doing so before would
  result in klipper crashing.

## [2024-12-22]

### Added
- **New Command: `SET_BUFFER_VELOCITY`**
    Allows users to tweak buffer velocity setting while printing. This setting is not
    saved in configuration.

    See command_reference doc for more info

- **New Command: `TEST_AFC_TIP_FORMING`**
    Gives ability to test AFC tip forming without doing a tool change

- **New Command: `RESET_AFC_MAPPING`**
    Resets all tool lane mapping to the order that is setup in configuration

### Fixed
- Fixed error in tip forming when `toolchange_temp` value is not zero

## [2024-12-20]

### Added
- More error printouts to aid users

### Fixes
- Misc error fixes

## [2024-12-13]

### Updated
- Updated Cut.cfg macro to have the ability to up stepper current when doing filament cutting, 
  see layer shift troubleshooting section on what values need to be set

## [2024-12-09]

### Added
- **New Command: `CALIBRATE_AFC`**  
    Allows calibration of the hub position and Bowden length in the Automated Filament Changer (AFC) system.  
    Supports calibration for a specific lane or all lanes (`LANE` parameter).  
    Provides options for distance and tolerance during calibration:
    - `DISTANCE=<distance>`: Optional distance parameter for lane movement during calibration (default is 25mm).
    - `TOLERANCE=<tolerance>`: Optional tolerance for fine-tuning adjustments during calibration (default is 5mm).  
    - Bowden Calibration: Added functionality to calibrate Bowden length for individual lanes using the `BOWDEN` parameter.

## [2024-12-08]

### Added
- When updating the AFC software, the `install-afc.sh` script will now remove any instances of `[gcode_macro T#]` found in the `AFC_Macros.cfg`
file as the code now generates them automatically.
- Added logic to pause print when filament goes past prep sensor. Verify that PAUSE macro move's toolhead off print when it's called.

## [2024-12-07]

### Updated
- When BT_TOOL_UNLOAD is used, spoolman active spool is set to None
- When spool is ejected from Box Turtle spoolman spool is removed from variables
- Activated espooler when user calls LANE_MOVE

### Fixed
- Fixed places where gcode was not referencing AFC and would cause crashes

## [2024-12-04]

### Fixed
- Fixed issue with Turtleneck buffer pins not being assigned correctly when prompted during install
- Fixed issue with LEDs not showing the right color when error happened during PREP
- Changed error message when AFC.vars.unit lane showed loaded but AFC.vars.tool file didn't match
- Added logic so that user could change trsync value. To set value add the following into `[AFC]` section in AFC.cfg file:  
`trsync_update: True`  
Optional:  
`trsync_timeout: 0.05`  
`TRSYNC_SINGLE_MCU_TIMEOUT: 0.5` 

## [2024-12-03]

### Added

- The `install-afc.sh` script will now query the printer upon exit to see if it is actively printing. If it is not
  printing, it will restart the `klipper` service.

## [2024-12-02]

### Added

- Buffer_Ram_Sensor
  - Enabling the buffer to be used as a ram sensor for loading and unloading filament
  - see Buffer_Ram_Sensor doc for more information

### Changed

- Adjusted load and unload to account for ram sensor
- Adjusted Prep to account for ram sensor

## [2024-11-27]

### Added
- `self.delay` to AFC_Prep to control delay time during Prep
  - Config option under `[AFC Prep]`, `delay_time: 1  # default .1`
  - This can be increased if TTC occurs during prep caused by H-bridge command queue

### Changed
- Simplified enabling and disabling of the buffer
- `AFC_extruder.py` now holds the functions and controls of the buffer
  - These common functions all called throughout

### Fixed
- Fixed erroring out if a buffer in not configured

## [2024-11-25]

### Changed
- Simplified buffer status to Trailing and Advancing
  - Buffer tube moving from Trailing to Advance it is in the Advancing state
  - Buffer tube moving from Advance to Trialing it is in the Trialing state

## [2024-11-27]

### Added

- `generate_docs.py` utility in the `utilities` folder to auto-generate some basic documentation in the `docs/command_reference.md` file.

## [2024-11-27]]

### Fixed
- Klipper erroring out when renaming `RESUME` macro when a user call's `BT_PREP` within the same reboot of klipper

## [2024-11-23]

### Added
- New buffer function `SET_BUFFER_MULTIPLIER` used to live adjust the high and low multipliers for the buffer
    - To change `multiplier_high`: `SET_BUFFER_MULTIPLIER MULTIPLIER=HIGH FACTOR=1.2`
    - To change `multiplier_low`: `SET_BUFFER_MULTIPLIER MULTIPLIER=HIGH FACTOR=0.8`
    - `MULTIPLIER` and `FACTOR` must be defined
    - Buffer config section must be updated for values to be saved

### Fixed
-Corrected buffer to only trigger when tube comes onto switch/sensor and not off

## [2024-11-22]

### Changed

*Full update, this needs more details*

## [2024-11-16]

### Added
- New variable `cut_servo_name` for AFC_hub configuration to specify which servo to use
- AFC_STATUS macro call, will print out what the current status is for each lane
  
  ex. 
  ```
  Turtle_1 Status
  LANE | Prep | Load | Hub | Tool |
  LEG1 |  xx  |  xx  |  x  |  xx  |
  LEG2 |  xx  |  xx  |  x  |  xx  |
  LEG3 |  xx  |  xx  |  x  |  xx  |
  LEG4 |  xx  |  xx  |  x  |  xx  |
  
  Turtle_2 Status
  LANE | Prep | Load | Hub | Tool |
  LEG5 |  xx  |  xx  |  x  |  xx  |
  LEG6 |  xx  |  xx  |  x  |  xx  |
  LEG7 |  xx  |  xx  |  x  |  xx  |
  LEG8 | <--> | <--> | <-> | <--> |
  ```

### Fixed
- Fixed hub_cut function to work with new structure
- Added sleeps back to hub_cut with reactor class

## [2024-11-03]

### Added

- Manually add the following section to your `AFC.cfg`
```
#--=================================================================================--#
######### Pause/Resume ################################################################
#--=================================================================================--#
xy_resume: False               # Enable to have position return to previous x,y coords after tool change
resume_speed: 0                # Speed of resume move. Leave 0 to use current printer speed
resume_z_speed: 0              # Speed of resume move in Z. Leave 0 to use current printer speed
```
- Manually add the following to your `AFC_macros.cfg`
```
[gcode_macro BT_RESUME]
description: Resume the print after an error
gcode:
    {% if not printer.pause_resume.is_paused %}
        RESPOND MSG="Print is not paused. Resume ignored"
    {% else %}
        AFC_RESUME
    {% endif %}
```
- If you encounter an error use the *BT_RESUME* macro to resume to the proper z height after the error is fixed.

### Changed

- Save/Restore position to use proper gcode location
- It will restore the z position first before making an x,y move

## [2024-10-31]

### Added

- Added LED buffer_indicator
  - allows for state change indication through color change
- Added AFC_buffer.md to layout the integration of a buffer into the AFC system

### Changed

- Changed buffer code to reflect buffer functionality and pin names
- Moved stepper commands from AFC_buffer to AFC_stepper
- Abstracted buffer status to be used in IP query and query buffer

## [2024-10-27]

### Added

- Updated the `install-afc.sh` script to include setup of the buffer configuration.
- Added `part_cooling_fan_speed` to poop macro
- Add `variable_part_cooling_fan_speed   : 1.0         # Speed to run fan when enabled above. 0 - 1.0` to your `_AFC_POOP_VARS` to change the value.

### Changed

- Broke the `install-afc.sh` script out into multiple files that are sourced by the main script for maintainability.

### Fixed
  - Fixed bug when `part_cooling_fan` was set to False

## [2024-10-24]

### Added

 - Added `loaded_to_hub` parameter to get_status so users can see if filament is loaded to  their hub
 - Added `SET_BOWDEN_LENGTH LENGTH={}` to change `afc_bowden_length`
      - Length can be changed in 3 ways:
          - An exact value can be set. `SET_BOWDEN_LENGTH LENGTH=955` will set the Bowden length to 955mm
          - Current value can be incremented positive or negative.
             - `SET_BOWDEN_LENGTH LENGTH=+100` if the original length was `955` it will be changed to `1055`
             - `SET_BOWDEN_LENGTH LENGTH=-100` if the original length was `955` it will be changed to `855`
       - `SET_BOWDEN_LENGTH` is called without a `LENGTH` specified then the value will be reset back to the configured length
       - Changed distance will have to be manually updated in `AFC.cfg`  

  - Added `Buffer_Name` to `AFC.cfg`
    - this allows the code base to have a name for the buffer to reference.
    - The name must match how buffer is defined in `[AFC_buffer *Buffer_Name*]`
    - ^^^This has to be manually updated and must be uncommented/added to AFC.cfg file^^^
  -  Additions to `AFC.py`
    - establish Buffer name
    - With buffer set up
      - Enable during `PREP`
      - Enable during `tool_load`
      - Disable during `tool_unload`
  - Added `SET_ROTATION_FACTOR` that uses variable `FACTOR`
    - if a turtleneck style buffer is enabled it will change the current rotation distance of the AFC stepper,
    - Values greater than 0
    - Values greater than 1 will cause more filament to be fed
    - Values Less than 1 greater than 0 will cause less filament to be fed
    
### Changed

 - Revamped `install-afc.sh` script to be interactive and provide more configuration options for the user.
 - Updated `ruff` GHA to only scan for changed files.
 - Updates to AFC.cfg file. Be sure to back up current file and replace with new version, then update values from backed up file.
 - Manually changes needed to AFC_hardware.cfg
    - `[filament_switch_sensor tool]` update to `[filament_switch_sensor tool_start]`
    - If using sensor after gears `[filament_switch_sensor extruder]` update to `[filament_switch_sensor tool_end]`

  - Full functionality change for Turtleneck/ Turtleneck 2.0 style buffers
  - Changed buffer configuration examples, new configuration is required for full functionality!
    - `multiplier_high` controls the speed-up of filament being fed
    - `multiplier_low` controls the slow-down of filament being fed
  - `QUERY_BUFFER` will output rotation distance if applicable

### Fixed

  - Minor adjustments to the use of single sensor buffers, retaining functionality for Belay

## [2024-08-28]

### Added

- Addition of two helper macros for the AFC system. 
  - `BT_LANE_EJECT` - This macro will eject a specified box turtle lane.
  - `BT_TOOL_UNLOAD` - This macro will unload a specified box turtle tool.

- Sample configuration files for the most popular boards are located in the `Klipper_cfg_example/AFC` directory.





























