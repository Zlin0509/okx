import Foundation
import Darwin

enum SecureCredentialFile {
    enum Stage { case created, published, written }
    enum SaveError: Error { case unsafeDirectory, ioFailure }

    // Temporary names contain no credential bytes. Publish the empty 0600 inode
    // first, then write via its descriptor only at the authorized final pathname.
    // A crash can leave an empty temp or a private incomplete final config, never
    // an extra plaintext temporary copy. The backend rejects incomplete JSON.
    static func write(_ data: Data, to destination: URL,
                      observe: ((Stage, URL) throws -> Void)? = nil) throws {
        let root = destination.deletingLastPathComponent()
        let directory = open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard directory >= 0 else { throw SaveError.unsafeDirectory }
        defer { close(directory) }
        var rootInfo = stat()
        guard fstat(directory, &rootInfo) == 0, rootInfo.st_uid == geteuid(),
              fchmod(directory, 0o700) == 0 else { throw SaveError.unsafeDirectory }
        // Share the backend's root lock: another running app/source instance must
        // prevent a configuration replacement while it owns this data directory.
        let lock = openat(directory, "desktop.lock", O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard lock >= 0 else { throw SaveError.ioFailure }
        defer { close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw SaveError.ioFailure }
        let name = ".demo-credentials.empty-" + UUID().uuidString
        let descriptor = openat(directory, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw SaveError.ioFailure }
        var published = false, complete = false
        defer {
            if published && !complete {
                var current = stat(), original = stat()
                if fstat(descriptor, &original) == 0,
                   fstatat(directory, destination.lastPathComponent, &current, AT_SYMLINK_NOFOLLOW) == 0,
                   current.st_ino == original.st_ino, current.st_dev == original.st_dev {
                    unlinkat(directory, destination.lastPathComponent, 0)
                }
            }
            unlinkat(directory, name, 0)
            close(descriptor)
        }
        // Observe immediately after creation, before any chmod could mask a
        // regression in the creation mode. No credential bytes exist here.
        try observe?(.created, root.appendingPathComponent(name))
        guard fchmod(descriptor, 0o600) == 0 else { throw SaveError.ioFailure }
        guard renameat(directory, name, directory, destination.lastPathComponent) == 0 else {
            throw SaveError.ioFailure
        }
        published = true
        try observe?(.published, destination)
        try data.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(descriptor, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw SaveError.ioFailure }
                offset += count
            }
        }
        try observe?(.written, destination)
        guard fsync(descriptor) == 0, fsync(directory) == 0 else { throw SaveError.ioFailure }
        complete = true
    }

    static func removeEmptyOrphans(in root: URL) {
        // Never inspect unrelated files or follow links; only our zero-byte temps.
        let lock = open(root.appendingPathComponent("desktop.lock").path,
                        O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        guard lock >= 0 else { return }
        defer { close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { return }
        guard let files = try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return }
        for file in files where file.lastPathComponent.hasPrefix(".demo-credentials.empty-") {
            var info = stat()
            if lstat(file.path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
               info.st_uid == geteuid(), info.st_size == 0, (info.st_mode & 0o077) == 0 {
                unlink(file.path)
            }
        }
    }
}
