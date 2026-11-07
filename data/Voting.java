package voting;
import java.util.Scanner;


public class Voting {

   
    public static void main(String[] args) {
        Scanner key=new Scanner(System.in);
        System.out.println("enter ur age");
        int age=key.nextInt();
        if(age<18){
            System.out.println("not allowed to vote");
        }
        else
        {
            System.out.println("allowed to vote");
        }
        key.close();
    }
    
}