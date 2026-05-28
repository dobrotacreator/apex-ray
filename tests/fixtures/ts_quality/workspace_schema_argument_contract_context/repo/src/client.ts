import { CoreBankPersonSchema } from './schemas.js';

interface HttpClient {
  get<T>(path: string, schema: { parse(value: unknown): T }): Promise<T>;
}

export async function loadPersonStatus(client: HttpClient, id: string): Promise<number> {
  const person = await client.get(`/clients/${id}`, CoreBankPersonSchema);
  return 0;
}
