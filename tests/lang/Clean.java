import java.util.List;
import java.util.Objects;

public final class Clean {

    private final List<Integer> values;

    public Clean(List<Integer> values) {
        this.values = Objects.requireNonNull(values, "values");
    }

    public int total() {
        int sum = 0;
        for (Integer value : values) {
            if (value != null) {
                sum += value;
            }
        }
        return sum;
    }

    public boolean isNamed(String name) {
        return "primary".equals(name);
    }
}
