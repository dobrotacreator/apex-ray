import 'package:flutter/material.dart';
import 'package:flutter_bloc/flutter_bloc.dart';
import 'package:mobx/mobx.dart';
import 'package:get_it/get_it.dart';
import 'package:go_router/go_router.dart';
import 'package:json_annotation/json_annotation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

@JsonSerializable()
class SampleModel {
  const SampleModel();
}

class SampleScreen extends StatefulWidget {
  const SampleScreen({super.key});

  @override
  State<SampleScreen> createState() => _SampleScreenState();
}

class _SampleScreenState extends State<SampleScreen> {
  final controller = TextEditingController();
  final storage = const FlutterSecureStorage();

  @override
  void initState() {
    super.initState();
  }

  Future<void> open() async {
    await Future<void>.delayed(Duration.zero);
    if (!mounted) return;
    context.go('/next');
  }

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => const SizedBox();
}

class SampleCubit extends Cubit<int> {
  SampleCubit() : super(0);
  void increment() => emit(state + 1);
}

class Store {
  @observable
  int count = 0;
}

final service = GetIt.I<SampleModel>();
final route = GoRoute(path: '/next', builder: (_, __) => const SampleScreen());
