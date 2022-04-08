#!/usr/bin/env python
# -*- coding: utf-8 -*-
# lsusb to check device name
#dmesg | grep "tty" to find port name

import serial,time


if __name__ == '__main__':
    
    print('Running. Press CTRL-C to exit.')
    with serial.Serial("/dev/ttyACM0", 115200, timeout=1) as arduino:
        time.sleep(0.1) #wait for serial to open
        if arduino.isOpen():
            print("{} connected!".format(arduino.port))
            try:
                while True:
                    cmd=input("Enter command : ")
                    arduino.writelines(cmd.encode())
                    time.sleep(0.1) #wait for arduino to answer
                    arduino.reset_output_buffer
                    while arduino.inWaiting()==0: pass
                    if  arduino.inWaiting()>0: 
                        answer=arduino.readlines(1000)
                        print(answer)
                        arduino.reset_input_buffer() #remove data after reading
            except KeyboardInterrupt:
                print("KeyboardInterrupt has been caught.")