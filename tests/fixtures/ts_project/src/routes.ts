type RequestLike = {
  path: string;
};

export function middleware(request: RequestLike) {
  if (
    request.path.startsWith("/api/webhook/")
  ) {
    return { cors: true };
  }
  return { cors: false };
}

export const config = {
  matcher: ["/api/webhook/:path*"],
};
