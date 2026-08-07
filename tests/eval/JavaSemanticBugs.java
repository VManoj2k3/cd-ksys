/** Labeled semantic bugs for the LLM layer — deterministic layers stay quiet.
 *  Used by tests/accuracy_eval.py to measure LLM recall on Java. */
public class JavaSemanticBugs {

    public static int average(int[] values) {
        int total = 0;
        for (int value : values) {
            total += value;
        }
        return total / values.length; // BUG line 10: ArithmeticException on empty array
    }

    public static int sumItems(int[] items) {
        int total = 0;
        for (int i = 0; i <= items.length; i++) { // BUG line 15: off by one
            total += items[i];
        }
        return total;
    }

    public static String readFirstLine(String path) throws java.io.IOException {
        java.io.BufferedReader reader =
            new java.io.BufferedReader(new java.io.FileReader(path));
        return reader.readLine(); // BUG line 24: reader never closed (leak)
    }

    public static String findFirst(java.util.List<String> names, String prefix) {
        for (String name : names) {
            if (name.startsWith(prefix)) {
                return name;
            }
        }
        return names.get(0); // BUG line 33: IndexOutOfBounds when list is empty
    }
}
