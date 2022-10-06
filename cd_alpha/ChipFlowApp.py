# !/usr/bin/python3py

# To execute remotely use:
# DISPLAY=:0.0 python3 ChipFlowApp.py

import contextlib
from collections import OrderedDict
import json
import os
from functools import partial
import serial
import time
from datetime import datetime
import logging
from cd_alpha.Device import Device
import kivy
from kivy.app import App
from kivy.lang import Builder
from kivy.uix.widget import Widget
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.popup import Popup
from kivy.clock import Clock
from kivy.properties import ObjectProperty, StringProperty, NumericProperty
from kivy.core.window import Window
from pkg_resources import resource_filename
from pathlib import Path
from cd_alpha.ProtocolFactory import JSONProtocolParser
from cd_alpha.protocols.protocol_tools import ProcessProtocol

Builder.load_file(resource_filename("cd_alpha", "gui-elements/widget.kv"))
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/roundedbutton.kv")
)
Builder.load_file(resource_filename("cd_alpha", "gui-elements/abortbutton.kv"))
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/useractionscreen.kv")
)
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/machineactionscreen.kv")
)
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/actiondonescreen.kv")
)
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/processwindow.kv")
)
Builder.load_file(resource_filename("cd_alpha", "gui-elements/progressdot.kv"))
Builder.load_file(resource_filename("cd_alpha", "gui-elements/circlebutton.kv"))
Builder.load_file(resource_filename("cd_alpha", "gui-elements/errorpopup.kv"))
Builder.load_file(resource_filename("cd_alpha", "gui-elements/abortpopup.kv"))
Builder.load_file(resource_filename("cd_alpha", "gui-elements/homescreen.kv"))
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/summaryscreen.kv")
)
Builder.load_file(
    resource_filename("cd_alpha", "gui-elements/protocolchooser.kv")
)

### UTIL FUNCTIONS ###


class ProcessScreenManager(ScreenManager):
    def __init__(self, *args, **kwargs):
        self.main_window = kwargs.pop("main_window")
        super().__init__(*args, **kwargs)

    def next_screen(self):
        current = self.current
        next_screen = self.next()

        # Don't go to protocol_chooser as a next step, go home instead
        if self.next() == "protocol_chooser":
            next_screen = "home"
        logging.debug(f"CDA: Next screen, going from {current} to {next_screen}")
        self.current = next_screen

    def next_step(self):
        """Propagate this command one level up."""
        self.main_window.next_step()

    def show_fatal_error(self, *args, **kwargs):
        self.main_window.show_fatal_error(*args, **kwargs)

    def start_over(self, dt):
        # TODO: any cleanup to be done?
        self.main_window.start_over()


class ChipFlowScreen(Screen):
    def __init__(self, *args, **kwargs):
        self.header_text = kwargs.pop("header", "Header")
        self.description_text = kwargs.pop("description", "Description.")
        super().__init__(**kwargs)

    def next_step(self, *args, **kwargs):
        self.parent.next_step()

    def show_fatal_error(self, *args, **kwargs):
        logging.debug("SCREEN: SFE")
        logging.debug(self)
        self.parent.show_fatal_error(*args, **kwargs)

    def start_over(self, dt):
        # TODO: any cleanup to be done?
        self.parent.start_over()


class UserActionScreen(ChipFlowScreen):
    def __init__(self, *args, **kwargs):
        self.next_text = kwargs.pop("next_text", "Next")
        super().__init__(*args, **kwargs)


class HomeScreen(ChipFlowScreen):
    def __init__(self, *args, **kwargs):
        self.next_text = kwargs.pop("next_text", "Next")
        super().__init__(*args, **kwargs)

    def load_protocol(self, *args, **kwargs):
        logging.info("Load button pressed!")
        self.manager.current = "protocol_chooser"


class MachineActionScreen(ChipFlowScreen):
    time_remaining_min = NumericProperty(0)
    time_remaining_sec = NumericProperty(0)
    progress = NumericProperty(0.0)

    def __init__(self, *args, **kwargs):
        self.action = kwargs.pop("action")
        self.time_total = 0
        self.time_elapsed = 0
        self.app = App.get_running_app()
        super().__init__(*args, **kwargs)

    # TODO this code is re-written multiple times and tied directly to GUI logic, desperately needs re-factor
    # TODO writing a protocol parser may help both loading and running protocols
    def start(self):
        WASTE_ADDR = self.app.WASTE_ADDR
        LYSATE_ADDR = self.app.LYSATE_ADDR
        for action, params in self.action.items():
            if action == "PUMP":
                if params["target"] == "waste":
                    addr = WASTE_ADDR
                if params["target"] == "lysate":
                    addr = LYSATE_ADDR
                rate_mh = params["rate_mh"]
                vol_ml = params["vol_ml"]
                eq_time = params.get("eq_time", 0)
                self.time_total = abs(vol_ml / rate_mh) * 3600 + eq_time
                self.time_elapsed = 0
                self._extracted_from_start_13("Addr = ", addr, rate_mh, vol_ml)
                self.app.scheduled_events.append(
                    Clock.schedule_interval(
                        self.set_progress, self.app.progressbar_update_interval
                    )
                )

            if action == "INCUBATE":
                self.time_total = params["time"]
                self.time_elapsed = 0
                self.app.scheduled_events.append(
                    Clock.schedule_interval(
                        self.set_progress, self.app.progressbar_update_interval
                    )
                )

            if action == "RESET":
                if self.app.device.DEVICE_TYPE == "R0":
                    logging.info(
                        "No RESET work to be done on the R0, passing to end of program"
                    )
                    return
                for addr in [self.app.WASTE_ADDR, self.app.LYSATE_ADDR]:
                    self.app.pumps.purge(1, addr)
                time.sleep(1)
                for addr in [self.app.WASTE_ADDR, self.app.LYSATE_ADDR]:
                    self.app.pumps.stop(addr)
                    self.app.pumps.purge(-1, addr)
                self.reset_stop_counter = 0
                self.app.scheduled_events.append(
                    Clock.schedule_interval(
                        partial(
                            self.switched_reset, "d2", WASTE_ADDR, 2, self.next_step
                        ),
                        self.app.switch_update_interval,
                    )
                )

                self.app.scheduled_events.append(
                    Clock.schedule_interval(
                        partial(
                            self.switched_reset, "d3", LYSATE_ADDR, 2, self.next_step
                        ),
                        self.app.switch_update_interval,
                    )
                )

            if action == "RESET_WASTE":
                if self.app.device.DEVICE_TYPE == "R0":
                    logging.info(
                        "No RESET work to be done on the R0, passing to end of program"
                    )
                    return
                for addr in [WASTE_ADDR]:
                    self.app.pumps.purge(1, addr)
                time.sleep(1)
                for addr in [WASTE_ADDR]:
                    self.app.pumps.stop(addr)
                    self.app.pumps.purge(-1, addr)
                self.reset_stop_counter = 0
                self.app.scheduled_events.append(
                    Clock.schedule_interval(
                        partial(
                            self.switched_reset, "d2", WASTE_ADDR, 1, self.next_step
                        ),
                        self.app.switch_update_interval,
                    )
                )

            if action == "GRAB":
                if self.app.POST_RUN_RATE_MM_CALIBRATION:
                    logging.debug("Using calibration post run rate values")
                    post_run_rate_mm = self.app.POST_RUN_RATE_MM_CALIBRATION
                else:
                    post_run_rate_mm = params["post_run_rate_mm"]
                if self.app.POST_RUN_VOL_ML_CALIBRATION:
                    logging.debug("Using calibration post run volume values")
                    post_run_vol_ml = self.app.POST_RUN_VOL_ML_CALIBRATION
                else:
                    post_run_vol_ml = params["post_run_vol_ml"]
                logging.debug(
                    f"Using Post Run Rate MM: {post_run_rate_mm}, ML : {post_run_vol_ml}"
                )

                for addr in [WASTE_ADDR, LYSATE_ADDR]:
                    logging.debug(f"CDA: Grabbing pump {addr}")
                    self.app.pumps.purge(1, addr)
                self.grab_stop_counter = 0
                swg1 = Clock.schedule_interval(
                    partial(
                        self.switched_grab,
                        "d4",
                        WASTE_ADDR,
                        2,
                        self.next_step,
                        post_run_rate_mm,
                        post_run_vol_ml,
                    ),
                    self.app.switch_update_interval,
                )

                self.app.scheduled_events.append(swg1)
                swg2 = Clock.schedule_interval(
                    partial(
                        self.switched_grab,
                        "d5",
                        LYSATE_ADDR,
                        2,
                        self.next_step,
                        post_run_rate_mm,
                        post_run_vol_ml,
                    ),
                    self.app.switch_update_interval,
                )

                self.app.scheduled_events.append(swg2)
                self.grab_overrun_check_schedule = Clock.schedule_once(
                    partial(self.grab_overrun_check, [swg1, swg2]),
                    self.app.grab_overrun_check_interval,
                )

                self.app.scheduled_events.append(self.grab_overrun_check_schedule)
            if action == "GRAB_WASTE":
                post_run_rate_mm = params["post_run_rate_mm"]
                post_run_vol_ml = params["post_run_vol_ml"]
                for addr in [WASTE_ADDR]:
                    logging.debug(f"CDA: Grabbing pump {addr}")
                    self.app.pumps.purge(1, addr)
                self.grab_stop_counter = 0
                swg1 = Clock.schedule_interval(
                    partial(
                        self.switched_grab,
                        "d4",
                        WASTE_ADDR,
                        1,
                        self.next_step,
                        post_run_rate_mm,
                        post_run_vol_ml,
                    ),
                    self.app.switch_update_interval,
                )

                self.app.scheduled_events.append(swg1)
                self.grab_overrun_check_schedule = Clock.schedule_once(
                    partial(self.grab_overrun_check, [swg1]),
                    self.app.grab_overrun_check_interval,
                )

                self.app.scheduled_events.append(self.grab_overrun_check_schedule)
            if action == "CHANGE_SYRINGE":
                diameter = params["diam"]
                pump_addr = params["pump_addr"]
                self.app.pumps.set_diameter(diameter, pump_addr)
                logging.debug(
                    f"Switching current loaded syringe to {diameter} diam on pump {pump_addr}"
                )

            if action == "RELEASE":
                if params["target"] == "waste":
                    addr = WASTE_ADDR
                if params["target"] == "lysate":
                    addr = LYSATE_ADDR
                rate_mh = params["rate_mh"]
                vol_ml = params["vol_ml"]
                eq_time = params.get("eq_time", 0)
                self._extracted_from_start_13(
                    "SENDING RELEASE COMMAND TO: Addr = ", addr, rate_mh, vol_ml
                )

    # TODO Rename this here and in `start`
    def _extracted_from_start_13(self, arg0, addr, rate_mh, vol_ml):
        logging.info(f"{arg0}{addr}")
        self.app.pumps.set_rate(rate_mh, "MH", addr)
        self.app.pumps.set_volume(vol_ml, "ML", addr)
        self.app.pumps.run(addr)

    def switched_reset(self, switch, addr, max_count, final_action, dt):
        if self.app.nano is None:
            raise IOError("No switches on the R0 should not be calling a switch reset!")
        self.app.nano.update()
        if not getattr(self.app.nano, switch):
            logging.info(f"CDA: Switch {switch} actived, stopping pump {addr}")
            self.app.pumps.stop(addr)
            self.reset_stop_counter += 1
            if self.reset_stop_counter == max_count:
                logging.debug(f"CDA: Both pumps homed")
                final_action()
            return False

    def switched_grab(
        self,
        switch,
        addr,
        max_count,
        final_action,
        post_run_rate_mm,
        post_run_vol_ml,
        dt,
    ):
        if self.app.nano is None:
            raise IOError("No switches on the R0 should not be calling switch grab!")
        self.app.nano.update()
        if not getattr(self.app.nano, switch):
            logging.info(f"CDA: Pump {addr} has grabbed syringe (switch {switch}).")
            logging.debug(
                f"CDA: Running extra {post_run_vol_ml} ml @ {post_run_rate_mm} ml/min to grasp firmly."
            )

            self.app.pumps.stop(addr)
            self.app.pumps.set_rate(post_run_rate_mm, "MM", addr)
            self.app.pumps.set_volume(post_run_vol_ml, "ML", addr)
            self.app.pumps.run(addr)
            self.grab_stop_counter += 1
            if self.grab_stop_counter == max_count:
                logging.debug("CDA: Both syringes grabbed")
                self.grab_overrun_check_schedule.cancel()
                final_action()
            return False

    def grab_overrun_check(self, swgs, dt):
        if self.app.nano is None:
            raise IOError(
                "No switches on the R0, should not be calling grab_overrrun_check!"
            )

        self.app.nano.update()
        overruns = []
        if getattr(self.app.nano, "d4"):
            overruns.append("1 (waste)")
            swgs[0].cancel()
        if getattr(self.app.nano, "d5"):
            overruns.append("2 (lysate)")
            swgs[1].cancel()
        if overruns:
            self.app.pumps.stop_all_pumps(self.app.list_of_pumps)
            overruns_str = " and ".join(overruns)
            plural = "s" if len(overruns) > 1 else ""
            logging.warning(f"CDA: Grab overrun in position{plural} {overruns_str}.")
            self.show_fatal_error(
                title=f"Syringe{plural} not detected",
                description=f"Syringe{plural} not inserted correctly in positions{plural} {overruns_str}.\nPlease start the test over.",
                confirm_text="Start over",
                confirm_action="abort",
                primary_color=(1, 0.33, 0.33, 1),
            )

    def set_progress(self, dt):
        self.time_elapsed += dt
        time_remaining = max(self.time_total - self.time_elapsed, 0)
        self.time_remaining_min = int(time_remaining / 60)
        self.time_remaining_sec = int(time_remaining % 60)
        self.progress = self.time_elapsed / self.time_total * 100
        if self.progress >= 100:
            self.progress = 100
            self.next_step()
            return False

    def on_enter(self):
        self.start()

    def skip(self):
        # Check that the motor is not moving
        # TODO make this work for pressure drive by checking if we've finished a step
        number_of_stopped_pumps = 0
        for pump in self.app.list_of_pumps:
            status = self.app.pumps.status(addr=pump)
            logging.info("Pump number {} status was: {}".format(pump, status))
            if status == "S":
                number_of_stopped_pumps += 1

        if number_of_stopped_pumps == len(self.app.list_of_pumps):
            logging.info("Skip button pressed. Moving to next step. ")
            Clock.unschedule(self.set_progress)
            self.next_step()
        else:
            logging.warning(
                "Pump not stopped! Step cannot be skipped while motors are moving. Not skipping. Status: {}".format(
                    status
                )
            )


class ActionDoneScreen(ChipFlowScreen):
    def __init__(self, *args, **kwargs):
        self.app = App.get_running_app()
        super().__init__(*args, **kwargs)
    def on_enter(self):
        self.app.pumps.buzz(repetitions=3, addr=self.app.WASTE_ADDR)
        self.app.scheduled_events.append(Clock.schedule_once(self.next_step, 1))


class FinishedScreen(ChipFlowScreen):
    def __init__(self, *args, **kwargs):
        self.app = App.get_running_app()
        super().__init__(*args, **kwargs)
    def on_enter(self):
        self.app.scheduled_events.append(Clock.schedule_once(self.start_over, 3))


class ProgressDot(Widget):
    status = StringProperty()

    def __init__(self, *args, **kwargs):
        index = kwargs.pop("index", None)
        self.status = "future"
        super().__init__(*args, **kwargs)

    def set_status(self, status):
        if status in ["past", "present", "future"]:
            self.status = status
        else:
            raise TypeError(
                "Status should be either of: 'past', 'present', 'future'. Got: '{}'".format(
                    status
                )
            )


class SteppedProgressBar(GridLayout):
    def __init__(self, *args, **kwargs):
        noof_steps = kwargs.pop("steps")
        super().__init__(
            *args, cols=noof_steps, padding=kwargs.pop("padding", 5), **kwargs
        )

        self.steps = [ProgressDot() for _ in range(noof_steps)]
        for s in self.steps:
            self.add_widget(s)
        self.position = 0
        self._update()

    def set_position(self, pos):
        self.position = pos
        self._update()

    def _update(self):
        # Check limits
        if self.position < 0:
            self.position = 0
        elif self.position > len(self.steps) - 1:
            self.position = len(self.steps) - 1
        # Set all steps to correct status
        for n, s in enumerate(self.steps):
            if self.position > n:
                s.set_status("past")
            elif self.position == n:
                s.set_status("present")
            elif self.position < n:
                s.set_status("future")


class ErrorPopup(Popup):
    description_text = StringProperty()
    confirm_text = StringProperty()
    confirm_action = ObjectProperty()
    primary_color = ObjectProperty((1, 0.33, 0.33, 1))

    def __init__(self, *args, **kwargs):
        self.description_text = kwargs.pop("description", "An error occurred")
        self.confirm_text = kwargs.pop("confirm_text", "An error occurred")
        self.confirm_action = kwargs.pop("confirm_action", lambda: print(2))
        self.primary_color = kwargs.pop("primary_color", (0.33, 0.66, 1, 1))
        super().__init__(*args, **kwargs)

    def confirm(self):
        logging.debug("CDA: Error acknowledged by user")
        self.disabled = True
        self.confirm_action()
        self.dismiss()


class AbortPopup(Popup):
    description_text = StringProperty()
    dismiss_text = StringProperty()
    confirm_text = StringProperty()
    confirm_action = ObjectProperty()
    primary_color = ObjectProperty((1, 0.33, 0.33, 1))

    def __init__(self, *args, **kwargs):
        self.description_text = kwargs.pop("description")
        self.dismiss_text = kwargs.pop("dismiss_text")
        self.confirm_text = kwargs.pop("confirm_text")
        self.confirm_action = kwargs.pop("confirm_action")
        self.primary_color = kwargs.pop("primary_color", (0.33, 0.66, 1, 1))
        super().__init__(*args, **kwargs)

    def confirm(self):
        logging.debug("CDA: Error acknowledged by user")
        self.disabled = True
        self.confirm_action()
        self.dismiss()


class ProtocolChooser(Screen):

    def __init__(self, **kw):
        self.app = App.get_running_app()
        super().__init__(**kw)

    def load(self, path, filename):
        try:
            filename = filename[0]
            logging.info(f"Filename List: {filename}")
        except Exception:
            return
        logging.info(f"Filename: {filename}  was chosen. Path: {path}")
        try:
            self.manager.main_window.load_protocol(filename)
        except BaseException as err:
            logging.error(f"Invalid Protocol: {filename}")
            logging.error(f"Unexpected Error: {err}, {type(err)}")

    def get_file_path(self):
        return self.app.PATH_TO_PROTOCOLS

    def cancel(self):
        logging.info("Cancel")
        self.manager.current = "home"


class SummaryScreen(Screen):
    def __init__(self, *args, **kwargs):
        self.next_text = kwargs.pop("next_text", "Next")
        self.header_text = kwargs.pop("header_text", "Summary")
        self.app = App.get_running_app()
        self.protocol_process = ProcessProtocol(self.app.PATH_TO_PROTOCOLS + self.app.PROTOCOL_FILE_NAME)
        super().__init__(*args, **kwargs)
        self.add_rows()

    def add_rows(self):
        """Return content of rows as one formatted string, roughly table shape."""
        summary_layout = self.ids.summary_layout
        for line in self.protocol_process.list_steps():
            for entry in line:
                summary_layout.add_widget(Label(text=str(entry)))


class CircleButton(Widget):
    pass


class RoundedButton(Widget):
    pass


class AbortButton(Button):
    pass


class LoadButton(Button):
    pass


class ProcessWindow(BoxLayout):
    def __init__(self, *args, **kwargs):
        self.protocol_file_name = kwargs.pop("protocol_file_name")
        super().__init__(*args, **kwargs)

        self.process_sm = ProcessScreenManager(main_window=self)
        self.progress_screen_names = []

        self.app = App.get_running_app()

        # TODO: break protocol loading into its own method
        protocol_location = self.app.PATH_TO_PROTOCOLS + self.protocol_file_name
        with open(protocol_location, "r") as f:
            protocol = json.loads(f.read(), object_pairs_hook=OrderedDict)

        protocol_obj = JSONProtocolParser(Path(protocol_location)).make_protocol()

        logging.info(protocol_obj)

        if self.app.START_STEP not in protocol.keys():
            raise KeyError("{} not a valid step in the protocol.".format(self.app.START_STEP))

        # if we're supposed to start at a step other than 'home' remove other steps from the protocol
        protocol_copy = OrderedDict()
        keep_steps = False
        for name, step in protocol.items():
            if name == self.app.START_STEP:
                keep_steps = True
            if keep_steps:
                protocol_copy[name] = step

        protocol = protocol_copy

        for name, step in protocol.items():
            screen_type = step.get("type", None)
            if screen_type == "UserActionScreen":
                if name == "home":
                    this_screen = HomeScreen(
                        name,
                        header=step.get("header", "NO HEADER"),
                        description=step.get("description", "NO DESCRIPTION"),
                        next_text=step.get("next_text", "Next"),
                    )

                elif name == "summary":
                    this_screen = SummaryScreen(next_text=step.get("next_text", "Next"))
                else:
                    this_screen = UserActionScreen(
                        name=name,
                        header=step.get("header", "NO HEADER"),
                        description=step.get("description", "NO DESCRIPTION"),
                        next_text=step.get("next_text", "Next"),
                    )
            elif screen_type == "MachineActionScreen":

                this_screen = MachineActionScreen(
                    name=name,
                    header=step["header"],
                    description=step.get("description", ""),
                    action=step["action"],
                )

                # TODO: clean up how this works
                if step.get("remove_progress_bar", False):
                    this_screen.children[0].remove_widget(
                        this_screen.ids.progress_bar_layout
                    )
                    this_screen.children[0].remove_widget(
                        this_screen.ids.skip_button_layout
                    )

                # Don't offer skip button in production
                if not self.app.DEBUG_MODE:
                    this_screen.children[0].remove_widget(
                        this_screen.ids.skip_button_layout
                    )
            else:
                if screen_type is None:
                    raise TypeError(
                        "Corrupt protocol. Every protocol step must contain a 'type' key."
                    )
                else:
                    raise TypeError(
                        "Corrupt protocol. Unrecognized 'type' key: {}".format(
                            screen_type
                        )
                    )
            self.progress_screen_names.append(this_screen.name)
            self.process_sm.add_widget(this_screen)

            completion_msg = step.get("completion_msg", None)
            if completion_msg:
                self.process_sm.add_widget(
                    ActionDoneScreen(
                        name=this_screen.name + "_done", header=completion_msg
                    )
                )

        self.overall_progress_bar = SteppedProgressBar(
            steps=len(self.progress_screen_names)
        )

        self.abort_btn = AbortButton(
            disabled=False, size_hint_x=None, on_release=self.show_abort_popup
        )
        protocol_chooser = ProtocolChooser(name="protocol_chooser")
        self.process_sm.add_widget(protocol_chooser)
        self.ids.top_bar.add_widget(self.overall_progress_bar)
        self.ids.top_bar.add_widget(self.abort_btn)
        self.ids.main.add_widget(self.process_sm)
        logging.info(
            "Widgets in process screen manager: {}".format(self.process_sm.screen_names)
        )

    def show_abort_popup(self, btn):
        popup_outside_padding = 60
        if self.process_sm.current == "home":
            abort_poup = AbortPopup(
                title="Shut down device?",
                description="Do you want to shut the device down? Once it has been shut down, you may safely turn it off with the switch located on the back side of the device.",
                dismiss_text="Cancel",
                confirm_text="Shut down",
                confirm_action=self.shutdown,
                primary_color=(0.33, 0.66, 1, 1),
                size_hint=(None, None),
                size=(800 - popup_outside_padding, 480 - popup_outside_padding),
            )
        else:
            abort_poup = AbortPopup(
                title="Abort entire test?",
                description="Do you want to abort the test? You will need to discard all single-use equipment (chip and syringes).",
                dismiss_text="Continue test",
                confirm_text="Abort test",
                confirm_action=self.abort,
                primary_color=(1, 0.33, 0.33, 1),
                size_hint=(None, None),
                size=(800 - popup_outside_padding, 480 - popup_outside_padding),
            )
        abort_poup.open()

    def abort(self):
        self.cleanup()
        self.process_sm.current = self.progress_screen_names[0]
        self.overall_progress_bar.set_position(0)

    def shutdown(self):
        self.cleanup()
        self.app.shutdown()

    def reboot(self):
        self.cleanup()
        self.app.reboot()

    def show_fatal_error(self, *args, **kwargs):
        logging.debug("CDA: Showing fatal error popup")
        popup_outside_padding = 60
        confirm_action = kwargs.pop("confirm_action", self.reboot)
        if confirm_action == "shutdown":
            confirm_action = self.shutdown
        if confirm_action == "reboot":
            confirm_action = self.reboot
        if confirm_action == "abort":
            confirm_action = self.abort
        error_window = ErrorPopup(
            title=kwargs.pop("header", "Fatal Error"),
            description=kwargs.pop(
                "description",
                "A fatal error occurred. Discard all used kit equipment and restart the test.",
            ),
            confirm_text=kwargs.pop("confirm_text", "Reboot now"),
            confirm_action=confirm_action,
            size_hint=(None, None),
            size=(800 - popup_outside_padding, 480 - popup_outside_padding),
            primary_color=kwargs.pop("primary_color", (0.33, 0.66, 1, 1)),
        )
        error_window.open()
        self.app.pumps.buzz(addr=self.app.WASTE_ADDR, repetitions=5)

    def start_over(self):
        logging.info("Sending Program to home screen")
        self.process_sm.current = "home"

    def next_step(self):
        self.process_sm.next_screen()
        if self.process_sm.current in self.progress_screen_names:
            pos = self.progress_screen_names.index(self.process_sm.current)
            self.overall_progress_bar.set_position(pos)

    def cleanup(self):
        # Global cleanup
        self.app.cleanup()
        # TODO: Any local cleanup?

    # TODO for testability instead of mutating the current App, we could return a Protocol object
    def load_protocol(self, path_to_protocol) -> ProcessScreenManager:
        
        load_protocol_screenmanager = ProcessScreenManager(main_window=self)

        with open(path_to_protocol, "r") as f:
            protocol = json.loads(f.read(), object_pairs_hook=OrderedDict)

        self.progress_screen_names = []

        # TODO: break protocol loading into its own method
        with open(path_to_protocol, "r") as f:
            protocol = json.loads(f.read(), object_pairs_hook=OrderedDict)

        if self.app.START_STEP not in protocol.keys():
            raise KeyError("{} not a valid step in the protocol.".format(self.app.START_STEP))

        # if we're supposed to start at a step other than 'home' remove other steps from the protocol
        protocol_copy = OrderedDict()
        keep_steps = False
        for name, step in protocol.items():
            if name == self.app.START_STEP:
                keep_steps = True
            if keep_steps:
                protocol_copy[name] = step

        protocol = protocol_copy

        for name, step in protocol.items():
            screen_type = step.get("type", None)
            if screen_type == "UserActionScreen":
                if name == "home":
                    this_screen = HomeScreen(
                        name,
                        header=step.get("header", "NO HEADER"),
                        description=step.get("description", "NO DESCRIPTION"),
                        next_text=step.get("next_text", "Next"),
                    )

                elif name == "summary":
                    this_screen = SummaryScreen(next_text=step.get("next_text", "Next"))
                else:
                    this_screen = UserActionScreen(
                        name=name,
                        header=step.get("header", "NO HEADER"),
                        description=step.get("description", "NO DESCRIPTION"),
                        next_text=step.get("next_text", "Next"),
                    )
            elif screen_type == "MachineActionScreen":

                this_screen = MachineActionScreen(
                    name=name,
                    header=step["header"],
                    description=step.get("description", ""),
                    action=step["action"],
                )
                # TODO: clean up how this works
                if step.get("remove_progress_bar", False):
                    this_screen.children[0].remove_widget(
                        this_screen.ids.progress_bar_layout
                    )
                    this_screen.children[0].remove_widget(
                        this_screen.ids.skip_button_layout
                    )

                # Don't offer skip button in production
                if not self.app.DEBUG_MODE:
                    this_screen.children[0].remove_widget(
                        this_screen.ids.skip_button_layout
                    )
            else:
                if screen_type is None:
                    raise TypeError(
                        "Corrupt protocol. Every protocol step must contain a 'type' key."
                    )
                else:
                    raise TypeError(
                        "Corrupt protocol. Unrecognized 'type' key: {}".format(
                            screen_type
                        )
                    )
            self.progress_screen_names.append(this_screen.name)
            self.process_sm.add_widget(this_screen)

            completion_msg = step.get("completion_msg", None)
            if completion_msg:
                self.process_sm.add_widget(
                    ActionDoneScreen(
                        name=this_screen.name + "_done", header=completion_msg
                    )
                )

        self.overall_progress_bar = SteppedProgressBar(
            steps=len(self.progress_screen_names)
        )

        self.abort_btn = AbortButton(
            disabled=False, size_hint_x=None, on_release=self.show_abort_popup
        )
        protocol_chooser = ProtocolChooser(name="protocol_chooser")
        self.process_sm.add_widget(protocol_chooser)
        self.ids.top_bar.add_widget(self.overall_progress_bar)
        self.ids.top_bar.add_widget(self.abort_btn)
        self.ids.main.add_widget(self.process_sm)
        logging.info(
            "Widgets in process screen manager: {}".format(self.process_sm.screen_names)
        )
        return load_protocol_screenmanager

    def screenduplicates(self, screen_names):
        list_of_screen_names = {}
        for name in screen_names:
            if name not in list_of_screen_names:
                list_of_screen_names[name] = 1
            else:
                list_of_screen_names[name] += 1
        return list_of_screen_names


class ChipFlowApp(App):
    def __init__(self, **kwargs):
        kivy.require("2.0.0")
        

        self.device = Device(resource_filename("cd_alpha", "device_config.json"))
        # Change the value in the config file to change which protocol is in use
        self.PROTOCOL_FILE_NAME = self.device.DEFAULT_PROTOCOL
        self.PATH_TO_PROTOCOLS = resource_filename("cd_alpha", "protocols/")
        self.DEBUG_MODE = self.device.DEBUG_MODE
        self.SERIAL_PATH = self.device.PUMP_SERIAL_ADDR
        self.DEV_MACHINE = self.device.DEV_MACHINE
        self.START_STEP = self.device.START_STEP
        self.POST_RUN_RATE_MM_CALIBRATION = self.device.POST_RUN_RATE_MM
        self.POST_RUN_VOL_ML_CALIBRATION = self.device.POST_RUN_VOL_ML

        # Branch below allows for the GUI App to be tested locally on a Windows machine without needing to connect the syringe pump or arduino

        # TODO fix these logic blocks using kivy built in OS testing
        # TODO fix file path issues using resource_filename() as seen above
        if self.DEV_MACHINE:
            LOCAL_TESTING = True
            time_now_str = (
                datetime.now().strftime("%Y-%m-%d_%H:%M:%S").replace(":", ";")
            )
            logging.basicConfig(
                filename=f"/home/pi/cd_alpha/logs/cda_{time_now_str}.log",
                filemode="w",
                datefmt="%Y-%m-%d_%H:%M:%S",
                level=logging.DEBUG,
            )
            logging.info("Logging started")
            from cd_alpha.software_testing.NanoControllerTestStub import Nano
            from cd_alpha.software_testing.NewEraPumpsTestStub import PumpNetwork
            from cd_alpha.software_testing.SerialStub import SerialStub

            SPLIT_CHAR = "\\"
        else:
            # Normal production mode
            from NanoController import Nano
            from NewEraPumps import PumpNetwork

            # For R0 debug
            Window.fullscreen = "auto"
            LOCAL_TESTING = False
            time_now_str = datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
            logging.basicConfig(
                filename=f"/home/pi/cd-alpha/logs/cda_{time_now_str}.log",
                filemode="w",
                datefmt="%Y-%m-%d_%H:%M:%S",
                level=logging.DEBUG,
            )
            logging.info("Logging started")
            SPLIT_CHAR = "/"

        # Establish serial connection to the pump controllers
        # TODO should be handled in an object not in a top level namespace
        if not LOCAL_TESTING:
            self.ser = serial.Serial("/dev/ttyUSB0", 19200, timeout=2)
        else:
            self.ser = SerialStub()
        self.pumps = PumpNetwork(self.ser)

        if self.DEBUG_MODE:
            logging.warning("CDA: *** DEBUG MODE ***")
            logging.warning("CDA: System will not reboot after exiting program.")

        logging.info(f"CDA: Using protocol: '{self.PROTOCOL_FILE_NAME}''")

        # Set constants
        if self.device.DEVICE_TYPE == "R0":
            self.WASTE_ADDR = self.device.PUMP_ADDR[0]
            self.WASTE_DIAMETER_mm = self.device.PUMP_DIAMETER[0]
        else:
            self.WASTE_ADDR = self.device.PUMP_ADDR[0]
            self.LYSATE_ADDR = self.device.PUMP_ADDR[1]
            self.WASTE_DIAMETER_mm = self.device.PUMP_DIAMETER[0]
            self.LYSATE_DIAMETER_mm = self.device.PUMP_DIAMETER[1]

        self.scheduled_events = []
        self.list_of_pumps = self.device.PUMP_ADDR

        # ---------------- MAIN ---------------- #

        logging.info("CDA: Starting main script.")

        self.nano = Nano(8, 7) if self.device.DEVICE_TYPE == "V0" else None

        # TODO why are magic numbers being defined mid initialization?
        self.progressbar_update_interval = 0.5
        self.switch_update_interval = 0.1
        self.grab_overrun_check_interval = 20

        super().__init__(**kwargs)

    def build(self):
        logging.debug("CDA: Creating main window")
        return ProcessWindow(protocol_file_name=self.PROTOCOL_FILE_NAME)

    def on_close(self):
        self.cleanup()
        if not self.DEBUG_MODE:
            self.reboot()
        else:
            logging.warning("DEBUG MODE: Not rebooting, just closing...")

    def cleanup(self):
        logging.debug("CDA: Cleaning upp")
        logging.debug("CDA: Unscheduling events")
        for se in self.scheduled_events:
            with contextlib.suppress(AttributeError):
                se.cancel()
        self.scheduled_events = []
        self.pumps.stop_all_pumps(self.list_of_pumps)

    def shutdown(self):
        logging.info("Shutting down...")
        self.cleanup()
        if self.DEBUG_MODE:
            logging.warning(
                "CDA: In DEBUG mode, not shutting down for real, only ending program."
            )
            App.get_running_app().stop()
        else:
            os.system("sudo shutdown --poweroff now")

    def reboot(self):
        self.cleanup()
        logging.info("CDA: Rebooting...")
        if self.DEBUG_MODE:
            logging.warning(
                "CDA: In DEBUG mode, not rebooting down for real, only ending program."
            )
            App.get_running_app().stop()
        else:
            os.system("sudo reboot --poweroff now")


def main():
    try:
        chip_app = ChipFlowApp()
        chip_app.run()
    except Exception:
        chip_app.cleanup()
        if not chip_app.DEBUG_MODE:
            ChipFlowApp.reboot()
        else:
            logging.warning("DEBUG MODE: Not rebooting, just re-raising error...")
            raise


if __name__ == "__main__":
    main()
