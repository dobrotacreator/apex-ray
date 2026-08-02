import Flutter

func configure(messenger: FlutterBinaryMessenger) {
  let channel = FlutterMethodChannel(name: "sample/profile", binaryMessenger: messenger)
  channel.setMethodCallHandler { call, result in
    switch call.method {
    case "refresh": result(nil)
    default: result(FlutterMethodNotImplemented)
    }
  }
}
