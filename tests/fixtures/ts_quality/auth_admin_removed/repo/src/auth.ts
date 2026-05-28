export type UserRole = "admin" | "support" | "member";

export interface User {
  id: string;
  role: UserRole;
  disabled: boolean;
}

export interface Project {
  id: string;
  ownerId: string;
}

export function canDeleteProject(user: User, project: Project): boolean {
  if (user.disabled) {
    return false;
  }

  return project.ownerId === user.id;
}
