import 'package:flutter/services.dart';

const channelName = 'sample/profile';
const channelFromEnvironment = String.fromEnvironment('SAMPLE_PROFILE_CHANNEL');
final profileChannel = MethodChannel(channelName);
final dynamicChannel = MethodChannel(channelFromEnvironment);

Future<void> refresh() async {
  await profileChannel.invokeMethod<void>('refresh');
}
