package com.example.orders;

import java.math.BigDecimal;
import java.util.List;

/** A customer order. */
public class Order {
    private Long id;
    private List<String> skus;
    private BigDecimal total;
}
