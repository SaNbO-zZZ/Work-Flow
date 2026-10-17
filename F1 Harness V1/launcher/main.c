// F1 Harness.app 的启动器（必须是 Mach-O：TCC 把"完全磁盘访问"授权绑定在 App 上，由它拉起的 Python 才能读外置硬盘）
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PORT 8790
#define URL "http://127.0.0.1:8790/"
#define ROOT "/Volumes/张三波个人硬盘/Codex/F1 Hermes V1"
#define SPARSE ROOT "/hermes-core.sparsebundle"
#define CORE "/Volumes/HermesCore"
#define PY CORE "/.venv/bin/python"
#define LOG CORE "/logs/launcher.log"

static int port_open(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons(PORT)};
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    int ok = connect(s, (struct sockaddr *)&a, sizeof a) == 0;
    close(s);
    return ok;
}

static int run(char *const argv[]) {
    pid_t pid = fork();
    if (pid == 0) { execv(argv[0], argv); _exit(127); }
    int st = 0;
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

static void dialog(const char *msg) {
    char script[1024];
    snprintf(script, sizeof script, "display dialog \"%s\" with title \"F1 Harness\" buttons {\"好\"} default button 1 with icon caution", msg);
    char *argv[] = {"/usr/bin/osascript", "-e", script, NULL};
    run(argv);
}

// 在子进程里读外置硬盘，带超时：没有完全磁盘访问权限时，后台进程读可移动卷会无限卡住而不是报错
static int can_read_drive(int timeout_s) {
    pid_t pid = fork();
    if (pid == 0) {
        struct stat st;
        _exit(stat(SPARSE "/Info.plist", &st) == 0 ? 0 : 1);
    }
    for (int i = 0; i < timeout_s * 10; i++) {
        int st;
        if (waitpid(pid, &st, WNOHANG) == pid) return WIFEXITED(st) && WEXITSTATUS(st) == 0;
        usleep(100000);
    }
    kill(pid, SIGKILL);
    waitpid(pid, NULL, 0);
    return 0;
}

int main(void) {
    if (port_open()) {  // 已在运行：只打开网页
        char *argv[] = {"/usr/bin/open", URL, NULL};
        run(argv);
        return 0;
    }
    // 开机自启时硬盘可能还没挂好，最多等 2 分钟
    struct stat st;
    for (int i = 0; i < 120 && stat(ROOT, &st) != 0; i++) sleep(1);
    if (stat(ROOT, &st) != 0) { dialog("没有找到移动硬盘「张三波个人硬盘」，请插好后再打开 F1 Harness。"); return 1; }
    if (!can_read_drive(8)) {
        dialog("F1 Harness 需要「完全磁盘访问权限」才能读移动硬盘。\\n请在接下来打开的设置里把 F1 Harness 打开，然后重新启动 F1 Harness。");
        char *argv[] = {"/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles", NULL};
        run(argv);
        return 1;
    }
    if (stat(PY, &st) != 0) {
        char *argv[] = {"/usr/bin/hdiutil", "attach", "-nobrowse", "-quiet", SPARSE, NULL};
        if (run(argv) != 0 || stat(PY, &st) != 0) { dialog("F1 Harness 磁盘映像挂载失败，请查看硬盘是否正常。"); return 1; }
    }

    // 服务作为独立子进程运行（仍由本 App 拉起，沿用它的磁盘权限）；启动器随后退出，再次双击才能走到上面的"已在运行"分支
    pid_t server = fork();
    if (server == 0) {
        setsid();
        int fd = open(LOG, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) { dup2(devnull, 0); close(devnull); }
        if (chdir(CORE) != 0) _exit(1);
        char *argv[] = {PY, "-m", "hermes.server", NULL};
        execv(PY, argv);
        _exit(127);
    }
    for (int i = 0; i < 60 && !port_open(); i++) sleep(1);
    if (!port_open()) { dialog("F1 Harness 服务没有启动成功，请查看 /Volumes/HermesCore/logs/launcher.log。"); return 1; }
    char *argv[] = {"/usr/bin/open", URL, NULL};
    run(argv);
    return 0;
}
