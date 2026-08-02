package sample

fun configureOther(messenger: BinaryMessenger, channelName: String) {
    MethodChannel(messenger, channelName)
}
