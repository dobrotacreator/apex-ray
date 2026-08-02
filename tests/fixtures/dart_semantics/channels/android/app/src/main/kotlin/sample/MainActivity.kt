package sample

private const val CHANNEL = "sample/profile"

fun configure(messenger: BinaryMessenger) {
    MethodChannel(messenger, CHANNEL).setMethodCallHandler { call, result ->
        when (call.method) {
            "refresh" -> result.success(null)
            "unrelated" -> result.notImplemented()
        }
    }
}
