# Automated Filament Control (AFC) Klipper Add-on

This Klipper plugin is for use with modern filament control systems such as BoxTurtle, NightOwl etc.

More information about BoxTurtle can be found [here](https://github.com/ArmoredTurtle/BoxTurtle)

Further information to include command references can be found [here](https://armoredturtle.xyz/docs/).

## Usage

Usage instructions for the `install-afc.sh` script can be shown by running:

```bash
./install-afc.sh -h
```

## Installation

To install this plugin, you should have the following pre-requisites installed:

1. Klipper
2. Moonraker
3. WebUI (Mainsail or Fluidd)
4. Both `jq` and `crudini` should be installed on your RaspPi (or equivalent). This can typically be accomplished with
    the following commands:
    
    ```bash
    sudo apt-get install jq crudini
    ```
5. Python >= 3.8
   

To install this plugin, you can use the following commands from the users home directory:

```bash
cd ~
git clone https://github.com/AFCProject/AFC-Klipper-Add-On.git
cd AFC-Klipper-Add-On
./install-afc.sh
```

Full options for the `install-afc.sh` script can be found by running the following command:

```bash
./install-afc.sh -h
```

## Updates

To update the AFC plugin software, you can simply run the following command:

```bash
cd AFC-Klipper-Add-On
./install-afc.sh
```

The update process should be non-destructive and will not overwrite any existing configuration files without your permission.
If you run into an issue due to a specific configuration, you may need to comment out the AFC plugin software and re-run the `install-afc.sh` script.

This can be accomplished by commenting out the following lines in your `printer.cfg` file:

```cfg
[include AFC/*.cfg]
```

Once the plugin is updated, please uncomment the lines in your `printer.cfg` file (if applicable).

## Documentation

Further documentation on the plugin, it's various commands and configuration references can be found [here](https://armoredturtle.xyz/docs/).

## Support

[![Join me on Discord](https://discord.com/api/guilds/1229586267671629945/widget.png?style=banner2)](https://discord.gg/eT8zc3bvPR)

Armored Turtle Configuration / Build Manuals [here](https://armoredturtle.xyz/)