function IsString(): PropertyDecorator { return () => undefined; }
function IsOptional(): PropertyDecorator { return () => undefined; }
function MaxLength(_value: number): PropertyDecorator { return () => undefined; }
export class RetriggerBodyDto {
  @IsString()
  actorId!: string;
  @MaxLength(500)
  reason?: string;
}
