import apex_ray.analyzers.dart.platform_channels as platform_channels_module
from apex_ray.analyzers.dart.platform_channels import (
    PlatformChannelIndex,
    extract_platform_channel_endpoints,
    platform_channel_contracts,
)


def test_platform_channel_contracts_match_only_opposite_directions() -> None:
    dart_source = """
import 'package:flutter/services.dart';

final bridge = MethodChannel('sample/bridge');

Future<void> configure() async {
  await bridge.invokeMethod<void>('dartRequest');
  await bridge.invokeMethod<void>('bothInvoke');
  bridge.setMethodCallHandler((call) async {
    switch (call.method) {
      case 'nativeCallback':
        return null;
      case 'bothHandle':
        return null;
    }
  });
}
"""
    native_source = """
fun configure(messenger: BinaryMessenger) {
  val bridge = MethodChannel(messenger, "sample/bridge")
  bridge.invokeMethod("nativeCallback", null)
  bridge.invokeMethod("bothInvoke", null)
  bridge.setMethodCallHandler { call, result ->
    when (call.method) {
      "dartRequest" -> result.success(null)
      "bothHandle" -> result.success(null)
    }
  }
}
"""
    endpoints = [
        *extract_platform_channel_endpoints("lib/bridge.dart", dart_source),
        *extract_platform_channel_endpoints("android/Bridge.kt", native_source),
    ]

    contracts = platform_channel_contracts(PlatformChannelIndex(tuple(endpoints)), "lib/bridge.dart")

    assert len(contracts) == 1
    assert "dartRequest" in contracts[0].text
    assert "nativeCallback" in contracts[0].text
    assert "bothInvoke" not in contracts[0].text
    assert "bothHandle" not in contracts[0].text


def test_platform_channel_extraction_ignores_comments_and_string_contents() -> None:
    source = r'''
import 'package:flutter/services.dart';

// final lineComment = MethodChannel('fake/line-comment');
/*
final blockComment = MethodChannel('fake/block-comment');
*/
const embeddedSource = """
final stringChannel = MethodChannel('fake/string');
bridge.invokeMethod<void>('fake/string-invoke');
bridge.setMethodCallHandler((call) async {
  switch (call.method) { case 'fake/string-handler-region': return null; }
});
""";

final bridge = MethodChannel('sample/real');

Future<void> configure() async {
  // await bridge.invokeMethod<void>('fake/comment-invoke');
  await bridge.invokeMethod<void>('realInvoke');
  bridge.setMethodCallHandler((call) async {
    // case 'fake/comment-handler': return null;
    const example = "case 'fake/string-handler': return null;";
    switch (call.method) {
      case 'realHandle':
        return null;
    }
  });
}
'''

    endpoints = extract_platform_channel_endpoints("lib/bridge.dart", source)

    assert [(endpoint.channel_name, endpoint.variable) for endpoint in endpoints] == [("sample/real", "bridge")]
    assert [(method.name, method.direction) for method in endpoints[0].methods] == [
        ("realInvoke", "invoke"),
        ("realHandle", "handle"),
    ]


def test_native_platform_channel_extraction_ignores_comments_and_raw_strings() -> None:
    source = r'''
fun configure(messenger: BinaryMessenger) {
  // val commentChannel = MethodChannel(messenger, "fake/comment")
  val embeddedSource = """
    val stringChannel = MethodChannel(messenger, "fake/string")
    bridge.invokeMethod("fake/string-invoke", null)
    bridge.setMethodCallHandler { call, result ->
      when (call.method) { "fake/string-handler" -> result.success(null) }
    }
  """

  val bridge = MethodChannel(messenger, "sample/real")
  // bridge.invokeMethod("fake/comment-invoke", null)
  bridge.invokeMethod("realInvoke", null)
  bridge.setMethodCallHandler { call, result ->
    // "fake/comment-handler" -> result.success(null)
    val example = "\"fake/string-handler\" -> result.success(null)"
    when (call.method) {
      "realHandle" -> result.success(null)
    }
  }
}
'''

    endpoints = extract_platform_channel_endpoints("android/Bridge.kt", source)

    assert [(endpoint.channel_name, endpoint.variable) for endpoint in endpoints] == [("sample/real", "bridge")]
    assert [(method.name, method.direction) for method in endpoints[0].methods] == [
        ("realInvoke", "invoke"),
        ("realHandle", "handle"),
    ]


def test_dart_platform_channel_extraction_keeps_nested_interpolation_code() -> None:
    source = r'''
import 'package:flutter/services.dart';

final bridge = MethodChannel('sample/interpolation');
final direct = "before ${bridge.invokeMethod<void>('ping')} after";
final escaped = "literal \${bridge.invokeMethod<void>('fake/escaped')}";
final simpleIdentifier = "$bridge.invokeMethod<void>('fake/simple')";
final simpleDeclaration = "$MethodChannel('fake/simple-channel')";
final crossedHandler = "$setMethodCallHandler((call) { ${(() {
  switch (call.method) { case 'fake/cross-region': return null; }
})()} })";
final nested = """
  outer ${(() {
    const hidden = "bridge.invokeMethod<void>('fake/string')";
    const rawHidden = r"${bridge.invokeMethod<void>('fake/raw')}";
    /* } bridge.invokeMethod<void>('fake/comment') { */
    final values = <String, Object?>{'key': {'nested': true}};
    bridge.setMethodCallHandler((call) async {
      switch (call.method) {
        case 'interpolationHandle':
          return null;
      }
    });
    return "inner ${bridge.invokeMethod<void>('nestedPing')}";
  })()}
""";
'''

    endpoints = extract_platform_channel_endpoints("lib/interpolation.dart", source)

    assert len(endpoints) == 1
    assert [(method.name, method.direction) for method in endpoints[0].methods] == [
        ("ping", "invoke"),
        ("interpolationHandle", "handle"),
        ("nestedPing", "invoke"),
    ]


def test_dart_lexer_exposes_simple_identifier_interpolation_only() -> None:
    source = "final value = 'literal MethodChannel(\\\"fake\\\") $bridge suffix';"

    code_positions = platform_channels_module._source_code_positions(source, deadline=None)

    interpolation = source.index("$bridge")
    fake_declaration = source.index("MethodChannel")
    assert all(code_positions[interpolation : interpolation + len("$bridge")])
    assert not any(code_positions[fake_declaration : fake_declaration + len("MethodChannel")])


def test_dart_interpolation_lexer_is_iterative_at_deep_nesting() -> None:
    depth = 1_500
    source = '"${' * depth + "bridge" + '}"' * depth

    code_positions = platform_channels_module._source_code_positions(source, deadline=None)

    identifier = source.index("bridge")
    assert all(code_positions[identifier : identifier + len("bridge")])
