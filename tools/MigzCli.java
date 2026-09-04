import com.linkedin.migz.MiGzInputStream;
import com.linkedin.migz.MiGzOutputStream;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/** Pipe stdin through MiGz, the way mzip and munzip would. */
public final class MigzCli {
    public static void main(String[] args) throws IOException {
        String mode = args.length > 0 ? args[0] : "";
        InputStream in = new BufferedInputStream(System.in, 1 << 20);
        OutputStream out = new BufferedOutputStream(System.out, 1 << 20);
        byte[] buf = new byte[1 << 20];
        if (mode.equals("c")) {
            MiGzOutputStream mz = new MiGzOutputStream(out);
            if (args.length > 1) {
                mz.setCompressionLevel(Integer.parseInt(args[1]));
            }
            for (int n; (n = in.read(buf)) > 0; ) {
                mz.write(buf, 0, n);
            }
            mz.close();
        } else if (mode.equals("d")) {
            MiGzInputStream mz = new MiGzInputStream(in);
            for (int n; (n = mz.read(buf)) > 0; ) {
                out.write(buf, 0, n);
            }
            mz.close();
        } else {
            System.err.println("usage: MigzCli c [level] | MigzCli d");
            System.exit(2);
        }
        out.flush();
    }
}
