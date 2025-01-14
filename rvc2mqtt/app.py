"""
Main app/entrypoint for RVC2MQTT

Thanks goes to the contributors of https://github.com/linuxkidd/rvc-monitor-py
This code is derived from parts of https://github.com/linuxkidd/rvc-monitor-py/blob/master/usr/bin/rvc2mqtt.py
which was licensed using Apache-2.0.  No copyright information was present in the above mentioned file but original
content is owned by the authors. 

Copyright 2022 Sean Brogan
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

import argparse
import logging
import logging.config
import queue
import signal
import time
import os
import ruyaml as YAML
from os import PathLike
import datetime
from typing import Optional
from rvc2mqtt.rvc import RVC_Decoder
from rvc2mqtt.can_support import CAN_Watcher
from rvc2mqtt.mqtt import MQTT_Support
from rvc2mqtt.plugin_support import PluginSupport
from rvc2mqtt.mqtt import *
from rvc2mqtt.entity_factory_support import entity_factory

PATH_TO_FOLDER = os.path.abspath(os.path.dirname(__file__))

def signal_handler(signal, frame):
    global MyApp
    logging.critical("shutting down.")
    MyApp.close()
    logging.shutdown()
    exit(0)


class app(object):
    def main(self, configuration: dict):
        """main function.  Sets up the app services, creates
        the receive thread, and processes messages.

        Runs until kill signal is sent
        """

        self.Logger = logging.getLogger("app")
        self.mqtt_client: MQTT_Support = None

        # make an receive queue of receive can bus messages
        self.rxQueue = queue.Queue()

        # For now lets buffer rVC formatted messages in this queue
        # which can then go thru the app to get encoded 
        # and put into the txQueue for the canbus
        # this is a little hacky...so need to revisit
        self.tx_RVC_Buffer = queue.Queue()


        # make a transmit queue to send can bus messages
        self.txQueue = queue.Queue()

        # thread to receive can bus messages
        self.receiver = CAN_Watcher(configuration["interface"]["name"], self.rxQueue, self.txQueue)
        self.receiver.start()

        # setup decoder
        self.rvc_decoder = RVC_Decoder()
        self.rvc_decoder.load_rvc_spec(os.path.join(PATH_TO_FOLDER, 'rvc-spec.yml'))  # load the RVC spec yaml

        # setup the mqtt broker connection
        if "mqtt" in configuration:
            self.mqtt_client = MqttInitalize(configuration["mqtt"])
            if self.mqtt_client:
                self.mqtt_client.client.loop_start()

        # Enable plugins
        self.PluginSupport: PluginSupport = PluginSupport(os.path.join(PATH_TO_FOLDER, "entity"), configuration.get("plugins", {}))

        # Use plugins to dynamically prepare the entity factory
        entity_factory_list = []
        self.PluginSupport.register_with_factory_the_entity_plugins(entity_factory_list)

        # setup entity list using 
        self.entity_list = []
        
        # initialize objects from the map list provided in config
        if "map" in configuration:
            for item in configuration["map"]:
                obj = entity_factory(item, self.mqtt_client, entity_factory_list)
                if obj is not None:
                    obj.set_rvc_send_queue(self.tx_RVC_Buffer)
                    obj.initialize()
                    self.entity_list.append(obj)

        # Our RVC message loop here
        while True:
            # process any received messages
            self.message_rx_loop()
            self.message_tx_loop()
            time.sleep(0.001)

    def close(self):
        """Shutdown the app and any threads"""
        if self.receiver:
            self.receiver.kill_received = True
        if self.mqtt_client is not None:
            self.mqtt_client.shutdown()
            self.mqtt_client.client.loop_stop()

    def message_tx_loop(self):
        """ hacky - translate RVC formatted dict from rvc_tx to canbus msg formatted tx"""
        if self.tx_RVC_Buffer.empty():
            return

        rvc_dict = self.tx_RVC_Buffer.get()

        #translate
        rvc_dict["arbitration_id"] = self.rvc_decoder._rvc_to_can_frame(rvc_dict)

        self.Logger.debug(f"Sending Msg: {str(rvc_dict)}")

        #put into canbus watcher
        self.txQueue.put(rvc_dict)

    def message_rx_loop(self):
        """Process any RVC received messages"""
        if self.rxQueue.empty():  # Check if there is a message in queue
            return

        message = self.rxQueue.get()

        try:
            MsgDict = self.rvc_decoder.rvc_decode(
                message.arbitration_id,
                "".join("{0:02X}".format(x) for x in message.data),
            )
        except Exception as e:
            self.Logger.warning(f"Failed to decode msg. {message}: {e}")
            return

        ## Log all rvc bus messages to custom logger so it can be routed or ignored
        logging.getLogger("rvc_bus_trace").debug(str(MsgDict))
        
        ## Find if this is a device entity in our list
        ## Pass to object

        for item in self.entity_list:
            if item.process_rvc_msg(MsgDict):
                ## Should we allow processing by more than one obj.  
                ## 
                return

        # Use a custom logger so it can be routed easily or ignored
        logging.getLogger("unhandled_rvc").debug(f"Msg {str(MsgDict)}")


def load_the_config(config_file_path: Optional[os.PathLike]):
    """ if config_file_path is a valid file load a yaml/json config file """
    if os.path.isfile(config_file_path):
        with open(config_file_path, "r") as content:
            yaml=YAML.YAML(typ='safe')
            return yaml.load(content.read())



if __name__ == "__main__":
    """Entrypoint.
    Get the config and run the app
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", dest="config_file_path", help="Config file path")
    args = parser.parse_args()

    config = load_the_config(args.config_file_path)

    try:
        logging.config.dictConfig(config["logger"])
    except Exception as e:
        print("Exception trying to setup loggers: " + str(e.args))
        print("Review https://docs.python.org/3/library/logging.config.html#logging-config-dictschema for details")

    logging.info(
        "Log Started: "
        + datetime.datetime.strftime(datetime.datetime.now(), "%A, %B %d, %Y %I:%M%p")
    )

    global MyApp
    MyApp = app()
    signal.signal(signal.SIGINT, signal_handler)
    MyApp.main(config)
