import java.util.List;
import java.util.Map; // unused import -> java AST check

public class Bad {

    // misspelled identifier -> spell layer
    private int recieveCount = 0;

    public boolean isAdmin(String role) {
        return role == "admin"; // string == compare -> java AST check
    }

    public int totalOf(List<Integer> items) {
        int total = 0;
        try {
            for (Integer item : items) {
                total += item;
            }
        } catch (Exception e) {
            // empty catch -> java AST check
        }
        return total;
    }

    public int getRecieveCount() {
        return recieveCount;
    }
}
