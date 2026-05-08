namespace OrderDomain;

public interface IOrderRepository
{
}

public class OrderService
{
    private readonly IOrderRepository _repo;

    public OrderService(IOrderRepository repo)
    {
        _repo = repo;
    }

    public decimal CalculateTotal(decimal subtotal, decimal discount)
    {
        if (discount < 0)
        {
            throw new ArgumentException("Desconto inválido");
        }

        return subtotal * (1 - discount);
    }
}
