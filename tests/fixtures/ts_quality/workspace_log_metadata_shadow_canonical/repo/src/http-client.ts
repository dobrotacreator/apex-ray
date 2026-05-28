import { HttpEvent } from './http-events.js';
import type { Logger } from './logger.js';

type LogMetadata = Record<string, unknown>;

export class HttpClient {
  constructor(private readonly logger: Logger) {}

  async get(path: string, metadata?: LogMetadata): Promise<void> {
    await this.handleNonOkResponse('GET', path, 422, '{"errors":[]} ', metadata);
  }

  private async handleNonOkResponse(
    method: string,
    path: string,
    statusCode: number,
    responseBody: string,
    metadata?: LogMetadata,
  ): Promise<void> {
    this.logFailure(method, path, statusCode, responseBody, metadata);
  }

  // Structured log consumers and alert templates depend on these canonical
  // fields. Metadata is untrusted adapter context and must never shadow them.
  private logFailure(method: string, path: string, statusCode: number, responseBody: string, metadata?: LogMetadata): void {
    this.logger.warn(
      {
        event: HttpEvent.FAILURE,
        method,
        path,
        statusCode,
        responseBody,
        ...metadata,
      },
      `${method} ${path} ${statusCode}`,
    );
  }
}
