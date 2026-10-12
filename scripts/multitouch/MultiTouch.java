import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;
import java.lang.reflect.Method;
import java.util.Arrays;

/** Runs as the ADB shell user through app_process; no APK or root needed. */
public final class MultiTouch {
    private final Object manager;
    private final Method inject;
    private final MotionEvent.PointerProperties[] properties;
    private final MotionEvent.PointerCoords[] coords;
    private final long downTime = SystemClock.uptimeMillis();

    private MultiTouch(int count) throws Exception {
        Class<?> type;
        try {
            type = Class.forName("android.hardware.input.InputManagerGlobal");
        } catch (ClassNotFoundException oldAndroid) {
            type = Class.forName("android.hardware.input.InputManager");
        }
        manager = type.getMethod("getInstance").invoke(null);
        inject = type.getMethod("injectInputEvent", InputEvent.class, int.class);
        properties = new MotionEvent.PointerProperties[count];
        coords = new MotionEvent.PointerCoords[count];
        for (int i = 0; i < count; i++) {
            properties[i] = new MotionEvent.PointerProperties();
            properties[i].id = i;
            properties[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].pressure = 1;
            coords[i].size = 1;
        }
    }

    private void send(int action, int count) throws Exception {
        MotionEvent event = MotionEvent.obtain(downTime, SystemClock.uptimeMillis(),
            action, count, Arrays.copyOf(properties, count), Arrays.copyOf(coords, count),
            0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            // WAIT_FOR_FINISH, matching Android's shell input command.
            if (!Boolean.TRUE.equals(inject.invoke(manager, event, 2))) {
                throw new IllegalStateException("Android rejected multi-touch input");
            }
        } finally {
            event.recycle();
        }
    }

    public static void main(String[] args) {
        MultiTouch touch = null;
        int active = 0;
        try {
            int duration = Integer.parseInt(args[0]);
            int count = (args.length - 1) / 4;
            if (duration < 1 || duration > 3000 || count < 2 || count > 10 ||
                args.length != 1 + count * 4) throw new IllegalArgumentException("Invalid gesture");
            int[][] paths = new int[count][4];
            for (int i = 0; i < count; i++) {
                for (int j = 0; j < 4; j++) {
                    paths[i][j] = Integer.parseInt(args[1 + i * 4 + j]);
                    if (paths[i][j] < 0 || paths[i][j] > 65535)
                        throw new IllegalArgumentException("Invalid coordinate");
                }
            }
            touch = new MultiTouch(count);
            for (int i = 0; i < count; i++) {
                touch.coords[i].x = paths[i][0];
                touch.coords[i].y = paths[i][1];
            }
            for (int i = 0; i < count; i++) {
                active = i + 1;
                touch.send(i == 0 ? MotionEvent.ACTION_DOWN :
                    MotionEvent.ACTION_POINTER_DOWN | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT), active);
            }
            long started = SystemClock.uptimeMillis();
            while (true) {
                long elapsed = SystemClock.uptimeMillis() - started;
                float fraction = Math.min(1f, (float) elapsed / duration);
                for (int i = 0; i < count; i++) {
                    touch.coords[i].x = paths[i][0] + (paths[i][2] - paths[i][0]) * fraction;
                    touch.coords[i].y = paths[i][1] + (paths[i][3] - paths[i][1]) * fraction;
                }
                touch.send(MotionEvent.ACTION_MOVE, count);
                if (fraction >= 1) break;
                SystemClock.sleep(Math.min(16, duration - elapsed));
            }
            for (int i = count - 1; i >= 0; i--) {
                touch.send(i == 0 ? MotionEvent.ACTION_UP :
                    MotionEvent.ACTION_POINTER_UP | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT), active);
                active--;
            }
        } catch (Exception error) {
            if (touch != null && active > 0) {
                try { touch.send(MotionEvent.ACTION_CANCEL, active); } catch (Exception ignored) {}
            }
            System.err.println("Multi-touch injection failed: " + error);
            System.exit(1);
        }
    }
}
