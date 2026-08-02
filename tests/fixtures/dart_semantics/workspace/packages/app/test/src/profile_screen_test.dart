import 'package:sample_app/src/profile_screen.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('profile screen', (tester) async {
    await tester.pumpWidget(const ProfileScreen());
  });
}
