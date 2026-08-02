import 'package:sample_app/src/profile_screen.dart';
import 'package:integration_test/integration_test.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('profile journey', (tester) async {
    await tester.pumpWidget(const ProfileScreen());
  });
}
