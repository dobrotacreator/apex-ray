import { RetriggerBodyDto } from './dto.js';
function Controller(_path: string): ClassDecorator {
  return () => undefined;
}
function Post(_path: string): MethodDecorator {
  return () => undefined;
}
function Param(_name: string): ParameterDecorator {
  return () => undefined;
}
function Body(_pipe?: unknown): ParameterDecorator {
  return () => undefined;
}
class ValidationPipe {}
@Controller('admin/webhooks/inbox')
export class AdminWebhookController {
  @Post(':id/retrigger')
  retrigger(
    @Param('id') id: string,
    @Body(new ValidationPipe()) body: RetriggerBodyDto,
  ): string {
    return id;
  }
}
