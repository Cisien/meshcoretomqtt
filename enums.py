from enum import Flag, Enum

class AdvertFlags(Flag):
    IsCompanion = 0x1
    IsRepeater = 0x2
    IsRoomServer = 0x3
    HasLocation = 0x10
    HasFuture1 = 0x20
    HasFuture2 = 0x30
    HasName = 0x80

class DeviceRole(Enum):
    Companion = 0x1
    Repeater = 0x2
    RoomServer = 0x3

class RouteType(Enum):
    TransportFlood = 0x0
    Flood = 0x1
    Direct = 0x2
    TransportDirect = 0x3

class PayloadType(Enum):
    Request = 0x00
    Response = 0x01
    TextMessage = 0x02
    Ack = 0x03
    Advert = 0x04
    GroupText = 0x05
    GroupData = 0x06
    AnonRequest = 0x07
    Path = 0x08
    Trace = 0x09
    Custom = 0x0F

class PayloadVersion(Enum):
    Version1 = 0x0
    Version2 = 0x1
    Version3 = 0x2
    Version4 = 0x3
