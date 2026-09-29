import { Nav } from "@/components/Nav";

/**
 * The signed-in shell.
 *
 * Nothing here checks the session: the middleware has already refused the request if there is no
 * valid cookie. Two places deciding who is allowed in is how one of them ends up wrong.
 */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="shell">
      <Nav />
      <div className="main">{children}</div>
    </div>
  );
}
