import pytest
from cd_alpha.Protocol import Protocol
from cd_alpha.Step import Step, ScreenType


class TestProtocol:
    def setUp(self):
        # import class and prepare everything here.
        self.test_protocol_location = "v0-protocol-16v1.json"

    
    def test_home_step(self):
        assert False



#{
#    "home": {
#        "type": "UserActionScreen",
#        "header": "Chip Diagnostics",
#        "description": "Ready for a new test with protocol 16v1. Press 'Start' to begin.",
#        "next_text": "Start"
#    }
#}