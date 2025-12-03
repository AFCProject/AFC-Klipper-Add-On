# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

class AFCprompt:
    def __init__(self, gcmd, logger):
        self.gcode = gcmd
        self.logger     = logger

    # Prompt begin action
    def p_begin(self, prompt_name):
        self.logger.raw("// action:prompt_begin {}".format(prompt_name))

    # Prompt text action
    def p_text(self, text):
        self.logger.raw("// action:prompt_text {}".format(text))

    # Prompt button action (with style options)
    def p_button(self, label, command, style=None):
        if style:
            self.logger.raw("// action:prompt_button {}|{}|{}".format(label, command, style))
        else:
            self.logger.raw("// action:prompt_button {}|{}".format(label, command))

    # Prompt footer button action (4 max allowed)
    def p_footer_button(self, label, command, style=None):
        if style:
            self.logger.raw("// action:prompt_footer_button {}|{}|{}".format(label, command, style))
        else:
            self.logger.raw("// action:prompt_footer_button {}|{}".format(label, command))

    def p_cancel_button(self):
        self.p_footer_button('Cancel', "RESPOND TYPE=command MSG=action:prompt_end", 'warning')

    # Show prompt action
    def p_show(self):
        self.logger.raw("// action:prompt_show")

    # Close prompt action
    def p_end(self):
        self.logger.raw("// action:prompt_end")

    # Button group start
    def p_button_group_start(self):
        self.logger.raw("// action:prompt_button_group_start")

    # Button group end
    def p_button_group_end(self):
        self.logger.raw("// action:prompt_button_group_end")

    # Method to create a custom prompt
    def create_custom_p(self, prompt_name, text=None, buttons=None, cancel=False, groups=None, footer_buttons=None):
        self.p_begin(prompt_name)

        if text is not None:
            self.p_text(text)

        # Add group buttons
        if groups is not None:
            for group in groups:
                self.p_button_group_start()
                for button in group:
                    label, command, style = button
                    self.p_button(label, command, style)
                self.p_button_group_end()

        # Add main buttons
        if buttons is not None:
            for button in buttons:
                label, command, style = button
                self.p_button(label, command, style)

        # Add footer buttons (if any)
        if footer_buttons is not None:
            for footer_button in footer_buttons:
                label, command, style = footer_button
                self.p_footer_button(label, command, style)

        if cancel:
            self.p_cancel_button()

        self.p_show()

    # template to be used to create prompts with groups of buttons
    def example_prompt(self, items):
        buttons = []
        group_buttons = []
        title = 'Prompt title'
        text = 'Prompt text'

        # Loop to group buttons
        for index, key in enumerate(items):
            # Create a button for each lane
            button_label = "{}".format(key)
            button_command = "CALIBRATE_AFC BOWDEN={}".format(key)
            button_style = "primary" if index % 2 == 0 else "secondary"
            group_buttons.append((button_label, button_command, button_style))

            # Add group to buttons list after every 4 keys
            if (index + 1) % 4 == 0 or index == len(items) - 1:
                buttons.append(group_buttons)
                group_buttons = []

        back = [('Back', '<Prompt to go back to>', 'info')]
        self.prompt.create_custom_p(title, text, None,
                                    True, buttons, back)
