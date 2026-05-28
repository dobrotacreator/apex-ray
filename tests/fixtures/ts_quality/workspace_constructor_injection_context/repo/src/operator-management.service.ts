function Injectable(): ClassDecorator {
  return () => undefined;
}

function Inject(_token: unknown): ParameterDecorator {
  return () => undefined;
}

export const OPERATOR_REPOSITORY_PORT = Symbol('OPERATOR_REPOSITORY_PORT');
export const AUDIT_REPOSITORY_PORT = Symbol('AUDIT_REPOSITORY_PORT');

export interface OperatorRepositoryPort {
  findById(id: string): Promise<string | null>;
}

@Injectable()
export class OperatorManagementService {
  constructor(@Inject(AUDIT_REPOSITORY_PORT) private readonly operators: OperatorRepositoryPort) {}

  async findOperator(id: string): Promise<string | null> {
    return this.operators.findById(id);
  }
}
