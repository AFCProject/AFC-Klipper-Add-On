## Further tests

=== "BoxTurtle"

    ### Respoolers

    Run the ``TEST`` command against each lane (one at a time) to verify proper respooler operation:

    - `TEST LANE=lane1`
    - `TEST LANE=lane2`
    - `TEST LANE=lane3`
    - `TEST LANE=lane4`

    Verify that each respooler works properly by moving in reverse like respooling a spool before proceeding.

    If your respoolers are operating in reverse (typically seen in a Formbot kit), you can simply reverse the pin assignments
    for the respoolers in `AFC_Turtle_1.cfg` file.

    e.g.

    ```cfg
    afc_motor_rwd: Turtle_1:MOT1_RWD
    afc_motor_fwd: Turtle_1:MOT1_FWD
    ```

    to
    ```cfg
    afc_motor_rwd: Turtle_1:MOT1_FWD
    afc_motor_fwd: Turtle_1:MOT1_RWD
    ```

    ### Trigger switches
    
    Actuating the trigger switch should begin pulsing that lane's extruder motor to load filament. Verify that the switch
    being actuated is activating that same lane's extruder motor. Between activating each trigger please allow 1-2 seconds
    before activating the next trigger.
    
    Once all lanes are confirmed to be activating the correct extruder when the trigger switch is actuated, move on to
    the next step.
    
    ### Extruders
    
    Insert filament into the feeder tube (it helps to cut it at an angle) and press through until the extruder motor gears
    catch the filament and load it further. If you can press the filament through, but it feels like the extruder motor is
    pushing back on the filament instead of pulling it in, try reversing the `dir_pin` setting for that extruder motor in
    your unit-specific config file (e.g., `AFC/AFC_Turtle_1.cfg` for BoxTurtle).
    
    If you are able to load filament into all lanes and get a green LED indicator, and the console
    reports no errors, move on to the next step.

=== "NightOwl"

    ### Prep sensor and motor direction
    Insert filament into a lane and keep feeding until the prep sensor is triggered, this should activate the correct stepper for that lane and the extruder gears should catch the filament and load it further until filament reaches the load sensor.

    If the extruder gears are not pulling in the filament and it feels like the extruder motor is pushing back on the filament, try reversing the `dir_pin` setting for that extruder motor in your unit-specific config file (e.g., `AFC/AFC_NightOwl_1.cfg`).

    If filament has triggered your prep sensor and your motor is not activated, check to make sure your load sensor didn't get swapped for the prep sensor.

    If you are able to load filament into all lanes, and the console reports no errors, move on to the next step.

=== "HTLF"

    --8<-- "includes/install/additional_tests_lane_selection.md"

    ### Homing Unit

    Run ``AFC_HOME_UNIT`` command to verify that unit moves selector back to home pin:

    - `AFC_HOME_UNIT UNIT=HTLF_1`

=== "HTLF2-Claymore"

    --8<-- "includes/install/additional_tests_lane_selection.md"

=== "ViViD"

    --8<-- "includes/install/additional_tests_lane_selection.md"

=== "EMU"

    ### For sensorless hub setups:
    Insert filament and verify that AFC moves filament to load sensor and then backs up the filament. Your load sensor in Fluidd/Mainsail should show it's
    not detected.


    ### For setups with hub sensor:
    Insert filament and verify that AFC moves filament towards your hub but not far enough to block the hub outlet. If filament is moved too far either run
    [AFC_CALIBRATION](../klipper/internal/calibration.md#AFC_functions.afcFunction.cmd_AFC_CALIBRATION) for that lane or update `dist_hub` before inserting filament in another lane.

    !!! note
        Please see additional note about EMUs default config setup [here](../configuration/AFC_UnitType_1.cfg.md#afc_emu-unit_name-section)

=== "ACE Pro 1/2"

    ### Load to hub

    Insert a spool into a lane. The ACE should feed filament to the hub itself; run
    `AFC_ACE_STATUS UNIT=Ace_1` (or `Ace2_1` for ACE Pro 2) and confirm that lane's slot reports as loaded.
    If it does not feed, or AFC reports an error, double check `serial_port`/`ace_index` and that
    `dist_hub` is set for the lane.

    !!! note
        The manual distance check below only applies if your hub does not have a switch (`switch_pin: virtual`).
        If your hub has a switch, run
        [AFC_CALIBRATION](../klipper/internal/calibration.md#AFC_functions.afcFunction.cmd_AFC_CALIBRATION) for
        each lane instead to automatically calibrate `dist_hub`.

    Watch the PTFE tube at the hub while it feeds. Filament should stop right at the hub, not go past it
    or push into it, and not stop short of it either. Eject the lane, then adjust that lane's `dist_hub`
    and reload it to test again:

    - Goes past/into the hub: decrease the length with `SET_HUB_DIST LANE=<lane_name> LENGTH=-<amount>`.
    - Stops short of the hub: increase the length with `SET_HUB_DIST LANE=<lane_name> LENGTH=+<amount>`.

    Repeat per lane until each one stops right before the hub. Once you're satisfied with the length,
    run `SAVE_HUB_DIST LANE=<lane_name>` for each lane to save the value to the config file.

    !!! note
        The next section, Buffer, does not apply to the ACE unless you've added an additional buffer
        into the ACE's PTFE path.

=== "Creality CFS"

    ### Spool detection

    Insert a spool into a slot. The box draws the filament in by itself, and the lane should show as ready in
    Mainsail/Fluidd. There is no hub distance to set or check: the box feeds until the toolhead sensor triggers.

    ### Cutter switch

    If you set `cutter_sensor_pin`, check it against your cut position. The CFS kit's cutter switch is an
    end-of-travel switch: it closes only when the blade has nearly finished its stroke. With nothing hot and the
    toolhead empty, after setting `pin_loc_xy` and `cut_direction` as on the [Macro variables](07-macro-variables.md)
    page:

    1. Home the printer and move the toolhead to about 6 mm before `pin_loc_xy`, on the side the cut starts from.
    1. Step toward the pin, 0.5 mm at a time near the end, and stop the moment the cutter arm touches it. If that
        isn't where `pin_loc_xy` puts it, correct `pin_loc_xy`.
    1. Keep stepping 0.5 mm at a time and run `AFC_CFS_STATUS UNIT=CFS_1` after each. Note the position where
        `cutter sensor` turns `triggered`. If it never does, try the inverted pin (`!` in front of the pin name).
    1. Set `cut_move_dist` so the full cut ends about 1 mm past the trigger point, then move the toolhead back
        away from the pin.
    1. With filament loaded by hand and the nozzle hot, run `AFC_CUT`. If the filament isn't cut through, raise
        `cut_move_dist` 0.3 mm at a time.

    ### Load and unload

    Home the printer, heat the nozzle and load lane 1 with `T0`. A load takes about 25 seconds once the nozzle is hot:
    the box feeds to the toolhead sensor, then the extruder makes two short pushes while the box hands the filament
    over. During the hand-off the loading slot flashes red for about two seconds; the load completes normally. Then
    change to another lane with `T1` and check that the cut, unload and reel-back into the box complete.

    If the extruder gears click during an unload, the retract after the cut is too long; see `unload_retract_steps`
    in the [configuration reference](../configuration/AFC_UnitType_1.cfg.md#afc_cfs-unit_name-section).

    !!! note
        The next section, Buffer, does not apply to the CFS. The CFS 4-in-1 buffer is managed by the box itself.

### Buffer

=== "Switched Buffer"
    Test that your buffer is configured correctly by extending the slide all the way out, then run
    `QUERY_BUFFER BUFFER=<configured_buffer_name>`. This should return `Trailing (buffer is compressing)`. Collapse the slide all the way so it
    triggers the
    switch at the rear, then rerun the QUERY_BUFFER command. It should then report `Advancing (buffer is expanding)`.
    To help understand the terminology:

    - Once the buffer fully expands, your AFC unit will reduce the speed of its stepper and the buffer will begin to compress.
    - Once the buffer fully compresses, your AFC unit will increase the speed of its stepper and the buffer will begin to expand.

    !!! note
        The buffer name in the `QUERY_BUFFER` command corresponds to the name configured during installation.
        For BoxTurtle, this is typically `Turtle_1`. Check your unit-specific config file for the correct buffer name.

    Confirm proper operation of your buffer before proceeding.
=== "FPS/PSF Buffer"
    To test that your buffer is configured correctly, while looking at the sensor in your Fluidd/Mainsail UI move the buffer until its in the "compressed" position.
    Verify that the PSF compressed sensor shows as detected. If expanded is being detected, set `reversed` variable to `True`. Restart klipper and verify that compressed
    is detected when moving to the compressed position, expanded is detected when in the expanded position and expanded/compressed sensors don't detect when
    in neutral position.
