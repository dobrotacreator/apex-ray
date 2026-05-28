import { canDeleteProject, Project, User } from "./auth";

export function deleteProject(user: User, project: Project): string {
  if (!canDeleteProject(user, project)) {
    throw new Error("Forbidden");
  }

  return `deleted:${project.id}`;
}
