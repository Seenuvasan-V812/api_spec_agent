package com.example.orders;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/orders")
public class OrderController {

    /** Fetch one order. */
    @GetMapping("/{id}")
    public Order get(@PathVariable long id) {
        return new Order();
    }

    /** Create an order. */
    @PostMapping
    public Order create(@RequestBody Order order) {
        return order;
    }
}
